"""
Perform imputation on a data frame.

For Copyright information, please see LICENCE.
"""
from decimal import Decimal
from enum import Enum
from functools import reduce
from typing import Optional, Union

from pyspark.sql import Column, DataFrame
from pyspark.sql.functions import col, expr, first, lit, when
from pyspark.sql.types import DecimalType, StringType

from statistical_methods_library.utilities.periods import (
    calculate_next_period,
    calculate_previous_period,
)
from statistical_methods_library.utilities.validation import validate_dataframe

from .ratio_calculators import RatioCalculator, construction

# --- Marker constants ---


class Marker(Enum):
    """Values for the marker column created during imputation."""

    RESPONSE = "R"
    """The value is a response."""

    FORWARD_IMPUTE_FROM_RESPONSE = "FIR"
    """The value has been forward imputed from a response."""

    BACKWARD_IMPUTE = "BI"
    """The value has been backward imputed from a response,
    backward imputation from construction is not permitted."""

    CONSTRUCTED = "C"
    """The value is constructed."""

    FORWARD_IMPUTE_FROM_CONSTRUCTION = "FIC"
    """The value has been forward imputed from a constructed value."""


def impute(
    *,
    input_df: DataFrame,
    reference_col: str,
    period_col: str,
    grouping_col: str,
    target_col: str,
    auxiliary_col: str,
    ratio_calculator: RatioCalculator,
    output_col: Optional[str] = "imputed",
    marker_col: Optional[str] = "imputation_marker",
    forward_link_col: Optional[str] = "forward",
    backward_link_col: Optional[str] = "backward",
    construction_link_col: Optional[str] = "construction",
    count_construction_col: Optional[str] = "count_construction",
    count_forward_col: Optional[str] = "count_forward",
    count_backward_col: Optional[str] = "count_backward",
    default_construction_col: Optional[str] = "default_construction",
    default_forward_col: Optional[str] = "default_forward",
    default_backward_col: Optional[str] = "default_backward",
    link_inclusion_current_col: Optional[str] = "link_inclusion_current",
    link_inclusion_previous_col: Optional[str] = "link_inclusion_previous",
    link_inclusion_next_col: Optional[str] = "link_inclusion_next",
    back_data_df: Optional[DataFrame] = None,
    link_filter: Optional[Union[str, Column]] = None,
    periodicity: Optional[int] = 1,
    weight: Optional[Decimal] = None,
    weight_periodicity_multiplier: Optional[int] = None,
    unweighted_forward_link_col: Optional[str] = "forward_unweighted",
    unweighted_backward_link_col: Optional[str] = "backward_unweighted",
    unweighted_construction_link_col: Optional[str] = "construction_unweighted",
    **ratio_calculator_params,
) -> DataFrame:
    # --- Validate params ---
    if not isinstance(input_df, DataFrame):
        raise TypeError("Input is not a DataFrame")

    link_cols = [forward_link_col, backward_link_col]
    if any(link_cols) and not all(link_cols):
        raise TypeError("Either all or no link columns must be specified")

    input_params = {
        "ref": reference_col,
        "period": period_col,
        "grouping": grouping_col,
        "target": target_col,
        "aux": auxiliary_col,
    }

    # Mapping of column aliases to parameters
    output_col_mapping = {
        "output": output_col,
        "marker": marker_col,
        "count_construction": count_construction_col,
        "count_forward": count_forward_col,
        "count_backward": count_backward_col,
        "default_construction": default_construction_col,
        "default_forward": default_forward_col,
        "default_backward": default_backward_col,
        "forward": forward_link_col,
        "backward": backward_link_col,
        "construction": construction_link_col,
        "ref": reference_col,
        "period": period_col,
        "grouping": grouping_col,
        "target": target_col,
    }

    if forward_link_col in input_df.columns or backward_link_col in input_df.columns:
        input_params.update(
            {
                "forward": forward_link_col,
                "backward": backward_link_col,
            }
        )

    if construction_link_col in input_df.columns:
        input_params["construction"] = construction_link_col

    back_input_params = {
        "ref": reference_col,
        "period": period_col,
        "grouping": grouping_col,
        "aux": auxiliary_col,
        "output": output_col,
        "marker": marker_col,
    }

    if weight is not None:
        if not isinstance(weight, Decimal):
            raise TypeError("weight must be of type Decimal")

        weight = lit(weight)
        weight_periodicity = weight_periodicity_multiplier * periodicity
        back_input_params.update(
            {
                "forward_unweighted": unweighted_forward_link_col,
                "backward_unweighted": unweighted_backward_link_col,
                "construction_unweighted": unweighted_construction_link_col,
            }
        )

        output_col_mapping.update(
            {
                "backward_unweighted": unweighted_backward_link_col,
                "forward_unweighted": unweighted_forward_link_col,
                "construction_unweighted": unweighted_construction_link_col,
            }
        )

    type_mapping = {
        "period": StringType,
        "target": DecimalType,
        "aux": DecimalType,
        "output": DecimalType,
        "marker": StringType,
        "forward": DecimalType,
        "backward": DecimalType,
        "construction": DecimalType,
        "forward_unweighted": DecimalType,
        "backward_unweighted": DecimalType,
        "construction_unweighted": DecimalType,
    }

    if link_filter:
        filtered_refs = (
            input_df.unionByName(back_data_df, allowMissingColumns=True).select(
                col(reference_col).alias("ref"),
                col(period_col).alias("period"),
                col(grouping_col).alias("grouping"),
                (
                    expr(link_filter) if isinstance(link_filter, str) else link_filter
                ).alias("match"),
            )
        ).localCheckpoint(eager=False)

    prepared_df = (
        validate_dataframe(
            input_df,
            input_params,
            type_mapping,
            ["ref", "period", "grouping"],
            ["target", "forward", "backward", "construction"],
        )
        .withColumnRenamed("target", "output")
        .withColumn("marker", when(~col("output").isNull(), Marker.RESPONSE.value))
        .withColumn(
            "previous_period", calculate_previous_period(col("period"), periodicity)
        )
        .withColumn("next_period", calculate_next_period(col("period"), periodicity))
    )
    prior_period_df = prepared_df.selectExpr(
        "min(previous_period) AS prior_period"
    ).localCheckpoint(eager=True)

    if back_data_df:
        validated_back_data_df = validate_dataframe(
            back_data_df,
            back_input_params,
            type_mapping,
            ["ref", "period", "grouping"],
        ).localCheckpoint(eager=False)
        back_data_period_df = (
            validated_back_data_df.select(
                "ref", "period", "grouping", "output", "aux", "marker"
            )
            .join(prior_period_df, [col("period") == col("prior_period")])
            .drop("prior_period")
            .filter(((col(marker_col) != lit(Marker.BACKWARD_IMPUTE.value))))
            .withColumn(
                "previous_period",
                calculate_previous_period(col("period"), periodicity),
            )
            .withColumn(
                "next_period", calculate_next_period(col("period"), periodicity)
            )
            .localCheckpoint(eager=False)
        )
        prepared_df = prepared_df.unionByName(
            back_data_period_df.filter(col("marker") == lit(Marker.RESPONSE.value)),
            allowMissingColumns=True,
        )

    def calculate_ratios():
        # This allows us to return early if we have nothing to do
        nonlocal prepared_df
        ratio_calculators = []
        if "forward" in prepared_df.columns:
            prepared_df = (
                prepared_df.withColumn("default_forward", expr("forward IS NULL"))
                .withColumn("default_backward", expr("backward IS NULL"))
                .fillna(1.0, ["forward", "backward"])
                .withColumn("count_forward", lit(0).cast("long"))
                .withColumn("count_backward", lit(0).cast("long"))
            )

        else:
            ratio_calculators.append(ratio_calculator)

        if "construction" in prepared_df.columns:
            prepared_df = (
                prepared_df.withColumn(
                    "default_construction", expr("construction IS NULL")
                )
                .fillna(1.0, ["construction"])
                .withColumn("count_construction", lit(0).cast("long"))
            )

        else:
            ratio_calculators.append(construction)

        if not ratio_calculators:
            return

        # Since we're going to join on to the main df filtering here
        # won't cause us to lose grouping as they'll just be filled with
        # default ratios.
        if link_filter:
            ratio_filter_df = prepared_df.join(
                filtered_refs, ["ref", "period", "grouping"]
            )
        else:
            ratio_filter_df = prepared_df.withColumn("match", lit(True))
        ratio_filter_df = ratio_filter_df.filter(
            ~ratio_filter_df.output.isNull()
        ).select(
            "ref",
            "period",
            "grouping",
            "output",
            "aux",
            "previous_period",
            "next_period",
            "match",
        )

        # Put the values from the current and previous periods for a
        # contributor on the same row.
        ratio_calculation_df = ratio_filter_df.alias("current")
        ratio_calculation_df = (
            ratio_calculation_df.join(
                ratio_filter_df.select(
                    "ref", "period", "output", "grouping", "match"
                ).alias("previous"),
                [
                    col("current.ref") == col("previous.ref"),
                    col("current.previous_period") == col("previous.period"),
                    col("current.grouping") == col("previous.grouping"),
                ],
                "leftouter",
            )
            .join(
                ratio_filter_df.select(
                    "ref", "period", "output", "grouping", "match"
                ).alias("next"),
                [
                    col("current.ref") == col("next.ref"),
                    col("current.next_period") == col("next.period"),
                    col("current.grouping") == col("next.grouping"),
                ],
                "leftouter",
            )
            .select(
                col("current.ref").alias("ref"),
                col("current.grouping").alias("grouping"),
                col("current.period").alias("period"),
                col("current.aux").alias("aux"),
                col("current.output"),
                col("current.match").alias("link_inclusion_current"),
                col("next.output"),
                col("next.match").alias("link_inclusion_next"),
                col("previous.output"),
                col("previous.match").alias("link_inclusion_previous"),
            )
        )

        # Join the grouping ratios onto the input such that each contributor has
        # a set of ratios.
        fill_values = {}
        for result in sum(
            (
                calculator(df=ratio_calculation_df, **ratio_calculator_params)
                for calculator in ratio_calculators
            ),
            [],
        ):
            prepared_df = prepared_df.join(result.data, result.join_columns, "left")
            fill_values.update(result.fill_values)
            output_col_mapping.update(result.additional_outputs)

        for fill_column, fill_value in fill_values.items():
            prepared_df = prepared_df.fillna(fill_value, fill_column)

        if link_filter:
            prepared_df = prepared_df.join(
                ratio_calculation_df.select(
                    "ref",
                    "period",
                    "grouping",
                    "link_inclusion_previous",
                    "link_inclusion_current",
                    "link_inclusion_next",
                ),
                ["ref", "period", "grouping"],
                "left",
            )
            output_col_mapping.update(
                {
                    "link_inclusion_current": link_inclusion_current_col,
                    "link_inclusion_previous": link_inclusion_previous_col,
                    "link_inclusion_next": link_inclusion_next_col,
                }
            )

        if weight is not None:

            def calculate_weighted_link(link_name):
                prev_link = col(f"prev.{link_name}")
                curr_link = col(f"curr.{link_name}")
                return (
                    when(
                        prev_link.isNotNull(),
                        weight * curr_link + (lit(Decimal(1)) - weight) * prev_link,
                    )
                    .otherwise(curr_link)
                    .alias(link_name)
                )

            weight_col_names = [
                name for name in ("forward", "backward", "construction")
                if name not in input_params
            ]

            if not weight_col_names:
                return

            weight_cols = [

            ]
            weighting_df = (
                prepared_df.select(
                    "period",
                    "grouping",
                    *(col(name).alias(f"{name}_unweighted")
                        for name in weight_col_names
                    )
                )
                .unionByName(
                    validated_back_data_df.select(
                        "period",
                        "grouping",
                        *(f"{name}_unweighted" for name in weight_col_names)
                    )
                )
                .groupBy("period", "grouping")
                .agg(
                    *(
                        first(f"{name}_unweighted").alias(name)
                        for name in weight_col_names
                    )
                )
            )

            curr_df = weighting_df.alias("curr")
            prev_df = weighting_df.alias("prev")
            prepared_df = (
                curr_df.join(
                    prev_df,
                    (
                        (
                            col("prev.period")
                            == calculate_previous_period(
                                col("curr.period"), weight_periodicity
                            )
                        )
                        & (col("curr.grouping") == col("prev.grouping"))
                    ),
                    "left",
                )
                .select(
                    expr("curr.period AS period"),
                    expr("curr.grouping AS grouping"),
                    *(
                        calculate_weighted_link(name)
                        for name in weight_col_names
                    )
                )
                .join(
                    reduce(
                        lambda d, n: d.withColumnRenamed(n, f"{n}_unweighted"),
                        weight_col_names,
                        prepared_df
                    ),
                    ["period", "grouping"],
                )
            )

    calculate_ratios()

    # Caching for both imputed and unimputed data.
    imputed_df = None
    null_response_df = None

    # --- Impute helper ---
    def impute_helper(
        df: DataFrame, link_col: str, marker: Marker, direction: bool
    ) -> DataFrame:
        nonlocal imputed_df
        nonlocal null_response_df
        if direction:
            # Forward imputation
            other_period_col = "previous_period"
        else:
            # Backward imputation
            other_period_col = "next_period"

        if imputed_df is None:
            working_df = df.select(
                "ref",
                "period",
                "grouping",
                "output",
                "marker",
                "previous_period",
                "next_period",
                "forward",
                "backward",
            )

            # Anything which isn't null is already imputed or a response and thus
            # can be imputed from. Note that in the case of backward imputation
            # this still holds since it always happens after forward imputation
            # and thus it can never attempt to backward impute from a forward
            # imputation since there will never be a null value directly prior to
            # one.
            imputed_df = working_df.filter(~col("output").isNull()).localCheckpoint(
                eager=True
            )
            # Any ref and grouping combos which have no values at all can't be
            # imputed from so we don't care about them here.
            ref_df = imputed_df.select("ref", "grouping").distinct()
            null_response_df = (
                working_df.filter(col("output").isNull())
                .drop("output", "marker")
                .join(ref_df, ["ref", "grouping"])
                .localCheckpoint(eager=True)
            )

        while True:
            other_df = imputed_df.selectExpr(
                "ref AS other_ref",
                "period AS other_period",
                "output AS other_output",
                "grouping AS other_grouping",
            )
            calculation_df = (
                null_response_df.join(
                    other_df,
                    [
                        col(other_period_col) == col("other_period"),
                        col("ref") == col("other_ref"),
                        col("grouping") == col("other_grouping"),
                    ],
                )
                .select(
                    "ref",
                    "period",
                    "grouping",
                    (col(link_col) * col("other_output")).alias("output"),
                    lit(marker.value).alias("marker"),
                    "previous_period",
                    "next_period",
                    "forward",
                    "backward",
                )
                .localCheckpoint(eager=False)
            )
            # If we've imputed nothing then we've got as far as we can get for
            # this phase.
            if calculation_df.count() == 0:
                break

            # Store this set of imputed values in our main set for the next
            # iteration. Use eager checkpoints to help prevent rdd DAG explosion.
            imputed_df = imputed_df.union(calculation_df).localCheckpoint(eager=True)
            # Remove the newly imputed rows from our filtered set.
            null_response_df = null_response_df.join(
                calculation_df.select("ref", "period", "grouping"),
                ["ref", "period", "grouping"],
                "leftanti",
            ).localCheckpoint(eager=True)

        # We should now have an output column which is as fully populated as
        # this phase of imputation can manage. As such replace the existing
        # output column with our one. Same goes for the marker column.
        return df.drop("output", "marker").join(
            imputed_df.select("ref", "period", "grouping", "output", "marker"),
            ["ref", "period", "grouping"],
            "leftouter",
        )

    # --- Imputation functions ---
    def forward_impute_from_response(df: DataFrame) -> DataFrame:
        if back_data_df:
            # Add the forward imputes from responses from the back data
            df = df.unionByName(
                back_data_period_df.filter(
                    col("marker") == lit(Marker.FORWARD_IMPUTE_FROM_RESPONSE.value)
                ),
                allowMissingColumns=True,
            )
        return impute_helper(df, "forward", Marker.FORWARD_IMPUTE_FROM_RESPONSE, True)

    def backward_impute(df: DataFrame) -> DataFrame:
        return impute_helper(df, "backward", Marker.BACKWARD_IMPUTE, False)

    # --- Construction functions ---
    def construct_values(df: DataFrame) -> DataFrame:
        if back_data_df:
            # Add in the constructions and forward imputes from construction in the back data
            df = df.unionByName(
                back_data_period_df.filter(
                    (
                        (col("marker") == lit(Marker.CONSTRUCTED.value))
                        | (
                            col("marker")
                            == lit(Marker.FORWARD_IMPUTE_FROM_CONSTRUCTION.value)
                        )
                    )
                ),
                allowMissingColumns=True,
            )

        construction_df = df.filter(df.output.isNull()).select(
            "ref", "period", "grouping", "aux", "construction", "previous_period"
        )
        other_df = df.select("ref", "period", "grouping").alias("other")
        construction_df = construction_df.alias("construction")
        construction_df = construction_df.join(
            other_df,
            [
                col("construction.ref") == col("other.ref"),
                col("construction.previous_period") == col("other.period"),
                col("construction.grouping") == col("other.grouping"),
            ],
            "leftanti",
        ).select(
            col("construction.ref").alias("ref"),
            col("construction.period").alias("period"),
            col("construction.grouping").alias("grouping"),
            (col("aux") * col("construction")).alias("constructed_output"),
            lit(Marker.CONSTRUCTED.value).alias("constructed_marker"),
        )

        return (
            df.withColumnRenamed("output", "existing_output")
            .withColumnRenamed("marker", "existing_marker")
            .join(
                construction_df,
                ["ref", "period", "grouping"],
                "leftouter",
            )
            .select(
                "*",
                when(col("existing_output").isNull(), col("constructed_output"))
                .otherwise(col("existing_output"))
                .alias("output"),
                when(col("existing_marker").isNull(), col("constructed_marker"))
                .otherwise(col("existing_marker"))
                .alias("marker"),
            )
            .drop("existing_output", "constructed_output", "constructed_marker")
        )

    def forward_impute_from_construction(df: DataFrame) -> DataFrame:
        # We need to recalculate our imputed and null response data frames to
        # account for construction.
        nonlocal imputed_df
        nonlocal null_response_df
        imputed_df = None
        null_response_df = None
        return impute_helper(
            df, "forward", Marker.FORWARD_IMPUTE_FROM_CONSTRUCTION, True
        )

    df = prepared_df
    for stage in (
        forward_impute_from_response,
        backward_impute,
        construct_values,
        forward_impute_from_construction,
    ):
        df = stage(df).localCheckpoint(eager=False)
        if df.filter(col("output").isNull()).count() == 0:
            break

    return df.join(prior_period_df, [col("prior_period") < col("period")]).select(
        [
            col(k).alias(output_col_mapping[k])
            for k in sorted(output_col_mapping.keys() & set(df.columns))
        ]
    )
