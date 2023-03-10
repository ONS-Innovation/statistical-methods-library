"""
Perform imputation on a data frame.

Currently only Ratio of Means (or Ratio of Sums) imputation is implemented.
For Copyright information, please see LICENCE.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, List, Optional, Union

from pyspark.sql import Column, DataFrame
from pyspark.sql.functions import col, lit, when
from pyspark.sql.types import DecimalType, IntegerType, StringType

from statistical_methods_library.utilities import validation

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


@dataclass
class RatioCalculationResult:
    data: DataFrame
    join_columns: List[Union[str, Column]]
    fill_columns: List[Union[str, Column]] = None


def impute(
    *,
    input_df: DataFrame,
    reference_col: str,
    period_col: str,
    strata_col: str,
    target_col: str,
    auxiliary_col: str,
    ratio_calculation_function: Callable[[DataFrame], Iterable[RatioCalculationResult]],
    output_col: Optional[str] = "imputed",
    marker_col: Optional[str] = "imputation_marker",
    forward_link_col: Optional[str] = None,
    backward_link_col: Optional[str] = None,
    construction_link_col: Optional[str] = None,
    count_construction_col: Optional[str] = "count_construction",
    count_forward_col: Optional[str] = "count_forward",
    count_backward_col: Optional[str] = "count_backward",
    back_data_df: Optional[DataFrame] = None,
    link_filter: Optional[Union[str, Column]] = None,
) -> DataFrame:
    # --- Validate params ---
    link_cols = [forward_link_col, backward_link_col, construction_link_col]
    if any(link_cols) and not all(link_cols):
        raise TypeError("Either all or no link columns must be specified")

    input_params = {
        "ref": reference_col,
        "period": period_col,
        "strata": strata_col,
        "target": target_col,
        "aux": auxiliary_col,
    }

    optional_params = {
        "forward": forward_link_col,
        "backward": backward_link_col,
        "construction": construction_link_col,
    }

    # Mapping of column aliases to parameters
    created_col_mapping = {
        "output": output_col,
        "marker": marker_col,
        "count_construction": count_construction_col,
        "count_forward": count_forward_col,
        "count_backward": count_backward_col,
    }

    new_link_map = {
        "forward": "forward",
        "backward": "backward",
        "construction": "construction",
    }

    if forward_link_col is not None:
        input_params.update(optional_params)

    full_col_mapping = {**input_params, **created_col_mapping}

    if forward_link_col is None:
        full_col_mapping = {**full_col_mapping, **new_link_map}

    back_expected_columns = {
        "ref": reference_col,
        "period": period_col,
        "strata": strata_col,
        "aux": auxiliary_col,
        "output": output_col,
        "marker": marker_col,
    }

    type_mapping = {
        "period": StringType,
        "target": DecimalType,
        "aux": DecimalType,
        "output": DecimalType,
        "marker": StringType,
        "forward": DecimalType,
        "backward": DecimalType,
        "construction": DecimalType,
    }

    aliased_input_df = validation.validate_dataframe(
        input_df,
        input_params,
        type_mapping,
        ["ref", "period"],
        ["target", "forward", "backward", "construction"],
    )

    # Cache the prepared back data df since we'll need a few differently
    # filtered versions
    prepared_back_data_df = None

    if back_data_df:
        prepared_back_data_df = validation.validate_dataframe(
            back_data_df, back_expected_columns, type_mapping, ["ref", "period"]
        )
        input_df = input_df.unionByName(back_data_df, allowMissingColumns=True)

    if link_filter:
        filtered_refs = input_df.filter(link_filter).select(
            col(reference_col).alias("ref"), col(period_col).alias("period")
        )

    # Store the value for the period prior to the start of imputation.
    # Stored as a value to avoid a join in output creation.
    prior_period = None

    # --- Run ---
    def run() -> DataFrame:
        nonlocal back_data_df

        def should_null_check(func, perform_null_check, *args, **kwargs):
            return lambda df: (func(df, *args, **kwargs), perform_null_check)

        stages = [
            should_null_check(prepare_df, False),
            should_null_check(calculate_ratios, True),
            should_null_check(forward_impute_from_response, True),
            should_null_check(backward_impute, True),
            should_null_check(construct_values, True),
            should_null_check(forward_impute_from_construction, True),
        ]
        df = aliased_input_df
        for stage in stages:
            result = stage(df)
            if result:
                df = result[0].localCheckpoint(eager=False)

                if result[1] and df.filter(col("output").isNull()).count() == 0:
                    break

        return create_output(df)

    # --- Prepare DF ---

    def prepare_df(df: DataFrame) -> DataFrame:
        nonlocal prepared_back_data_df

        prepared_df = (
            df.withColumn("output", col("target"))
            .drop("target")
            .withColumn("marker", when(~col("output").isNull(), Marker.RESPONSE.value))
            .withColumn("previous_period", calculate_previous_period(col("period")))
            .withColumn("next_period", calculate_next_period(col("period")))
        )

        nonlocal prior_period
        # We know this will be a single value so use collect as then we
        # can filter directly.
        prior_period = prepared_df.selectExpr("min(previous_period)").collect()[0][0]

        if prepared_back_data_df:
            prepared_back_data_df = (
                prepared_back_data_df.filter(
                    (
                        (col(period_col) == lit(prior_period))
                        & (col(marker_col) != lit(Marker.BACKWARD_IMPUTE.value))
                    )
                )
                .withColumn("previous_period", calculate_previous_period(col("period")))
                .withColumn("next_period", calculate_next_period(col("period")))
            )

        else:
            # Set the prepared_back_data_df to be empty when back_data not
            # supplied.
            prepared_back_data_df = prepared_df.filter(col(period_col).isNull())

        prepared_back_data_df = prepared_back_data_df.localCheckpoint(eager=True)
        # Ratio calculation needs all the responses from the back data
        prepared_df = prepared_df.unionByName(
            filter_back_data(col("marker") == lit(Marker.RESPONSE.value)),
            allowMissingColumns=True,
        )

        return prepared_df

    # --- Calculate Ratios ---

    def calculate_ratios(df: DataFrame) -> DataFrame:
        if "forward" in df.columns:
            df = (
                df.fillna(1.0, ["forward", "backward", "construction"])
                .withColumn("count_forward", lit(None).cast("long"))
                .withColumn("count_backward", lit(None).cast("long"))
                .withColumn("count_construction", lit(None).cast("long"))
            )
            return df

        # Since we're going to join on to the main df at the end filtering here
        # won't cause us to lose strata as they'll just be filled with
        # default ratios.
        if link_filter:
            filtered_df = df.join(filtered_refs, ["ref", "period"])
        else:
            filtered_df = df
        filtered_df = filtered_df.filter(~df.output.isNull()).select(
            "ref",
            "period",
            "strata",
            "output",
            "aux",
            "previous_period",
            "next_period",
        )

        # Put the values from the current and previous periods for a
        # contributor on the same row.
        working_df = filtered_df.alias("current")
        working_df = working_df.join(
            filtered_df.select("ref", "period", "output", "strata").alias("prev"),
            [
                col("current.ref") == col("prev.ref"),
                col("current.previous_period") == col("prev.period"),
                col("current.strata") == col("prev.strata"),
            ],
            "leftouter",
        ).select(
            col("current.strata").alias("strata"),
            col("current.period").alias("period"),
            when(~col("prev.output").isNull(), col("current.output")).alias("output"),
            col("current.aux").alias("aux"),
            col("prev.output").alias("other_output"),
            col("current.output").alias("output_for_construction"),
            col("current.next_period").alias("next_period"),
        )

        # Join the strata ratios onto the input such that each contributor has
        # a set of ratios.
        all_fill_cols = []
        for result in ratio_calculation_function(working_df):
            df = df.join(result.data, result.join_columns, "left")
            if result.fill_columns:
                all_fill_cols += result.fill_columns

        if all_fill_cols:
            df = df.fillna(1.0, all_fill_cols)

        return df

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
                "strata",
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
            # Any refs which have no values at all can't be imputed from so we
            # don't care about them here.
            ref_df = imputed_df.select("ref").distinct()
            null_response_df = (
                working_df.filter(col("output").isNull())
                .drop("output", "marker")
                .join(ref_df, "ref")
                .localCheckpoint(eager=True)
            )

        while True:
            other_df = imputed_df.selectExpr(
                "ref AS other_ref",
                "period AS other_period",
                "output AS other_output",
                "strata AS other_strata",
            )
            calculation_df = (
                null_response_df.join(
                    other_df,
                    [
                        col(other_period_col) == col("other_period"),
                        col("ref") == col("other_ref"),
                        col("strata") == col("other_strata"),
                    ],
                )
                .select(
                    "ref",
                    "period",
                    "strata",
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
                calculation_df.select("ref", "period"), ["ref", "period"], "leftanti"
            ).localCheckpoint(eager=True)

        # We should now have an output column which is as fully populated as
        # this phase of imputation can manage. As such replace the existing
        # output column with our one. Same goes for the marker column.
        return df.drop("output", "marker").join(
            imputed_df.select("ref", "period", "output", "marker"),
            ["ref", "period"],
            "leftouter",
        )

    # --- Imputation functions ---
    def forward_impute_from_response(df: DataFrame) -> DataFrame:
        # Add the forward imputes from responses from the back data
        df = df.unionByName(
            filter_back_data(
                col("marker") == lit(Marker.FORWARD_IMPUTE_FROM_RESPONSE.value)
            ),
            allowMissingColumns=True,
        )
        return impute_helper(df, "forward", Marker.FORWARD_IMPUTE_FROM_RESPONSE, True)

    def backward_impute(df: DataFrame) -> DataFrame:
        return impute_helper(df, "backward", Marker.BACKWARD_IMPUTE, False)

    # --- Construction functions ---
    def construct_values(df: DataFrame) -> DataFrame:
        # Add in the constructions and forward imputes from construction in the back data
        df = df.unionByName(
            filter_back_data(
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
            "ref", "period", "strata", "aux", "construction", "previous_period"
        )
        other_df = df.select("ref", "period", "strata").alias("other")
        construction_df = construction_df.alias("construction")
        construction_df = construction_df.join(
            other_df,
            [
                col("construction.ref") == col("other.ref"),
                col("construction.previous_period") == col("other.period"),
                col("construction.strata") == col("other.strata"),
            ],
            "leftanti",
        ).select(
            col("construction.ref").alias("ref"),
            col("construction.period").alias("period"),
            (col("aux") * col("construction")).alias("constructed_output"),
            lit(Marker.CONSTRUCTED.value).alias("constructed_marker"),
        )

        return (
            df.withColumnRenamed("output", "existing_output")
            .withColumnRenamed("marker", "existing_marker")
            .join(
                construction_df,
                ["ref", "period"],
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

    # --- Utility functions ---
    def create_output(df: DataFrame) -> DataFrame:
        del full_col_mapping["aux"]
        del full_col_mapping["strata"]
        return select_cols(
            df.filter(col("period") != lit(prior_period)), reversed=False
        ).withColumnRenamed("output", output_col)

    def select_cols(
        df: DataFrame,
        reversed: bool = True,
        drop_unmapped: bool = True,
        mapping: dict = full_col_mapping,
    ) -> DataFrame:
        col_mapping = {v: k for k, v in mapping.items()} if reversed else mapping
        col_set = set(df.columns)

        return df.select(
            [
                col(k).alias(col_mapping.get(k, k))
                for k in ((col_mapping.keys() & col_set) if drop_unmapped else col_set)
            ]
        )

    def calculate_previous_period(period: Column) -> Column:
        return when(
            period.endswith("01"), (period.cast(IntegerType()) - 89).cast(StringType())
        ).otherwise((period.cast(IntegerType()) - 1).cast(StringType()))

    def calculate_next_period(period: Column) -> Column:
        return when(
            period.endswith("12"), (period.cast(IntegerType()) + 89).cast(StringType())
        ).otherwise((period.cast(IntegerType()) + 1).cast(StringType()))

    def filter_back_data(filter_col: Column) -> DataFrame:
        return prepared_back_data_df.filter(filter_col).localCheckpoint(eager=True)

    # ----------

    return run()