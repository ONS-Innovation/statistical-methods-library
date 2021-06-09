from pyspark.sql import DataFrame
from pyspark.sql.functions import col, lit, when

# --- Marker constants ---
# The value is a response.
MARKER_RESPONSE = "R"
# The value has been forward imputed from a response.
MARKER_FORWARD_IMPUTE_FROM_RESPONSE = "FIR"
# The value has been backward imputed from a response, backward imputation
# from construction is not permitted.
MARKER_BACKWARD_IMPUTE = "BI"
# The value is constructed.
MARKER_CONSTRUCTED = "C"
# The value has been forward imputed from a constructed value.
MARKER_FORWARD_IMPUTE_FROM_CONSTRUCTION = "FIC"


# --- Imputation errors ---
# Base type for imputation errors
class ImputationError(Exception):
    pass


# Error raised by dataframe validation
class ValidationError(ImputationError):
    pass


# Error raised when imputation has failed to impute for data integrity reasons
class DataIntegrityError(ImputationError):
    pass


def imputation(
    input_df,
    reference_col,
    period_col,
    strata_col,
    target_col,
    auxiliary_col,
    output_col,
    marker_col,
    forward_link_col=None,
    backward_link_col=None,
    construction_link_col=None,
):
    """
    Perform Ratio of means (also known as Ratio of Sums) imputation on a
    dataframe.

    :param input_df: The input dataframe - an instance of pyspark.sql.Dataframe
    :param reference_col: The name of the column to reference a unique
    contributor - str
    :param period_col: The name of the column containing the period
    information for the contributor - str
    :param strata_col: The Name of the column containing the strata information
    for the contributor - str
    :param target_col: The name of the column containing the target
    variable - str
    :param auxiliary_col: The name of the column containing the auxiliary
    variable - str
    :param output_col: The name of the column which will contain the
    output - str
    :param marker_col: The name of the column which will contain the marker
    information for a given value - str
    :param forward_link_col: If specified, the name of an existing column
    containing forward ratio (or link) information - str or None
    Defaults to None which means that a default column name of "forward" will
    be created and the forward ratios will be calculated
    :param backward_link_col: If specified, the name of an existing column
    containing backward ratio (or link) information - str or None
    Defaults to None which means that a default column name of "backward"
    will be created and the backward ratios will be calculated
    :param construction_link_col: If specified, the name of an existing column
    containing construction ratio (or link) information - str or None
    Defaults to None which means that a default column name of "construction"
    will be created and the construction ratios will be calculated.

    Returns:
    A new dataframe containing:
    * <reference_col>
    * <period_col>
    * <strata_col>
    * <target_col>
    * <auxiliary_col>
    * <output_col>
    * <marker_col>
    * <forward_col>
    * <backward_col>
    * <construction_col>

    Where the <...> refer to the provided column names or the respective
    defaults. No other columns are created. In particular, no other columns
    will be passed through from the input since it is expected that the
    information in the output dataframe will be sufficient to join on any
    other required input data.

    Notes:
    The existence of <output_col> and <marker_col> in the input data is
    an error.

    All or none of <forward_link_col>, <backward_link_col> and
    <construction_link_col> must be specified.

    <marker_col> will contain one of the marker constants defined in this
    module.

    This method implements rolling imputation, that is imputed values chain
    together until either a return is present or the contributor is not present
    in the sample. Values either side of such a gap do not interact (i.e.
    ratios and imputes will not take into account values for a contributor
    from before or after periods where they were dropped from the sample). In
    the case of rolling imputation, the markers will be the same for chains of
    imputed values.
    """
    # --- Validate params ---
    if not isinstance(input_df, DataFrame):
        raise TypeError("input_df must be an instance of pyspark.sql.DataFrame")

    def run(df):
        validate_df(df)
        stages = (
            prepare_df,
            calculate_ratios,
            forward_impute_from_response,
            backward_impute,
            construct_values,
            forward_impute_from_construction,
        )

        for stage in stages:
            df = stage(df).localCheckpoint()
            if df.filter(col("output").isNull()).count() == 0:
                break

        return create_output(df)

    def validate_df(df):
        input_cols = set(df.columns)
        expected_cols = {
            reference_col,
            period_col,
            strata_col,
            target_col,
            auxiliary_col,
        }

        link_cols = [
            link_col is not None
            for link_col in [forward_link_col, backward_link_col, construction_link_col]
        ]

        if any(link_cols) and not all(link_cols):
            raise TypeError("Either all or no link columns must be specified")

        if forward_link_col is not None:
            expected_cols.add(forward_link_col)
            expected_cols.add(backward_link_col)
            expected_cols.add(construction_link_col)

        # Check to see if the col names are not blank.
        for col_name in expected_cols:
            if not isinstance(col_name, str):
                msg = "All column names provided in params must be strings."
                raise TypeError(msg)

            if col_name == "":
                msg = "Column name strings provided in params must not be empty."
                raise ValueError(msg)

        # Check to see if any required columns are missing from dataframe.
        missing_cols = expected_cols - input_cols
        if missing_cols:
            msg = f"Missing columns: {', '.join(c for c in missing_cols)}"
            raise ValidationError(msg)

    def prepare_df(df):
        col_list = [
            col(reference_col).alias("ref"),
            col(period_col).alias("period"),
            col(strata_col).alias("strata"),
            col(target_col).alias("target"),
            col(auxiliary_col).alias("aux"),
            col(target_col).alias("output"),
        ]

        if forward_link_col is not None:
            col_list += [
                col(forward_link_col).alias("forward"),
                col(backward_link_col).alias("backward"),
                col(construction_link_col).alias("construction"),
            ]

        prepared_df = df.select(col_list)
        return (
            prepared_df.withColumn(
                "marker", when(~col("output").isNull(), MARKER_RESPONSE)
            )
            .withColumn("previous_period", calculate_previous_period(col("period")))
            .withColumn("next_period", calculate_next_period(col("period")))
        )

    def create_output(df):
        nonlocal forward_link_col
        if forward_link_col is None:
            forward_link_col = "forward"

        nonlocal backward_link_col
        if backward_link_col is None:
            backward_link_col = "backward"

        nonlocal construction_link_col
        if construction_link_col is None:
            construction_link_col = "construction"

        select_col_list = [
            col("ref").alias(reference_col),
            col("period").alias(period_col),
            col("strata").alias(strata_col),
            col("target").alias(target_col),
            col("aux").alias(auxiliary_col),
            col("output").alias(output_col),
            col("marker").alias(marker_col),
            col("forward").alias(forward_link_col),
            col("backward").alias(backward_link_col),
            col("construction").alias(construction_link_col),
        ]
        if "forward" not in df.columns:
            # If we haven't calculated forward ratio we've not calculated any
            df = (
                df.withColumn("forward", lit(1.0))
                .withColumn("backward", lit(1.0))
                .withColumn("construction", lit(1.0))
            )

        return df.select(select_col_list)

    def calculate_previous_period(period):
        return when(
            period.endswith("01"), (period.cast("int") - 89).cast("string")
        ).otherwise((period.cast("int") - 1).cast("string"))

    def calculate_next_period(period):
        return when(
            period.endswith("12"), (period.cast("int") + 89).cast("string")
        ).otherwise((period.cast("int") + 1).cast("string"))

    def calculate_ratios(df):
        ratio_union_df = None
        # Since we're going to join on to the main df at the end filtering for
        # nulls won't cause us to lose strata as they'll just be filled with
        # default ratios.
        filtered_df = (
            df.filter(~df.output.isNull())
            .select(
                "ref",
                "period",
                "strata",
                "output",
                "aux",
                "previous_period",
                "next_period",
            )
            .persist()
        )
        for strata_val in filtered_df.select("strata").distinct().toLocalIterator():
            strata_df = filtered_df.filter(df.strata == strata_val["strata"]).persist()
            period_df = (
                strata_df.select("period", "previous_period", "next_period")
                .distinct()
                .persist()
            )
            strata_forward_union_df = None
            for period_val in period_df.toLocalIterator():
                period = period_val["period"]
                previous_period = period_val["previous_period"]
                df_current_period = (
                    strata_df.filter(strata_df.period == period)
                    .alias("current")
                    .persist()
                )
                df_previous_period = (
                    strata_df.filter(strata_df.period == previous_period)
                    .alias("prev")
                    .persist()
                )
                # Put the values from the current and previous periods for a
                # contributor on the same row. Then calculate the sum for both
                # for all contributors in a period as the values now line up.
                working_df = df_current_period.join(
                    df_previous_period,
                    (col("current.ref") == col("prev.ref")),
                    "leftouter",
                ).select(
                    when(~col("prev.output").isNull(), col("current.output")).alias(
                        "output"
                    ),
                    col("current.aux").alias("aux"),
                    col("prev.output").alias("other_output"),
                    col("current.output").alias("output_for_construction"),
                )
                working_df = (
                    working_df.agg(
                        {
                            "output": "sum",
                            "other_output": "sum",
                            "aux": "sum",
                            "output_for_construction": "sum",
                        }
                    )
                    .withColumn("period", lit(period))
                    .withColumn("next_period", lit(period_val["next_period"]))
                )

                # Calculate the forward ratio for every period using 1 in the
                # case of a 0 denominator. We also calculate construction
                # links at the same time for efficiency reasons. This shares
                # the same behaviour of defaulting to a 1 in the case of a 0
                # denominator.
                working_df = working_df.withColumn(
                    "forward",
                    col("sum(output)")
                    / when(col("sum(other_output)") == 0, lit(1.0)).otherwise(
                        col("sum(other_output)")
                    ),
                ).withColumn(
                    "construction",
                    col("sum(output_for_construction)")
                    / when(col("sum(aux)") == 0, lit(1.0)).otherwise(col("sum(aux)")),
                )

                # Store the completed period.
                working_df = working_df.select(
                    "period", "forward", "construction", "next_period"
                )

                if strata_forward_union_df is None:
                    strata_forward_union_df = working_df.persist()

                else:
                    strata_forward_union_df = strata_forward_union_df.union(
                        working_df
                    ).persist()

            strata_forward_union_df = strata_forward_union_df.fillna(
                1.0, ["forward", "construction"]
            ).persist()

            strata_forward_union_df = (
                period_df.select("period", "next_period")
                .join(strata_forward_union_df, "period", "leftouter")
                .select(
                    period_df.period,
                    period_df.next_period,
                    strata_forward_union_df,
                    forward,
                    strata_forward_union_df.construction,
                )
            )
            # Calculate backward ratio as 1/forward for the next period.
            strata_backward_df = (
                strata_forward_union_df.join(
                    strata_forward_union_df.select(
                        col("next_period").alias("period"),
                        col("forward").alias("next_forward"),
                    ),
                    "period",
                    "leftouter",
                )
                .select("*", (lit(1.0) / col("next_forward")).alias("backward"))
                .select("period", "forward", "backward", "construction")
            )
            strata_joined_df = (
                period_df.join(strata_backward_df, "period", "leftouter")
                .select("period", "forward", "backward", "construction")
                .fillna(1.0, "backward")
            )
            strata_ratio_df = strata_joined_df.withColumn(
                "strata", lit(strata_val["strata"])
            )

            # Store the completed ratios for this strata.
            if ratio_union_df is None:
                ratio_union_df = strata_ratio_df.persist()
            else:
                ratio_union_df = ratio_union_df.union(strata_ratio_df).persist()

        # Join the strata ratios onto the input such that each contributor has
        # a set of ratios.
        ret_df = df.join(ratio_union_df, ["period", "strata"]).select(
            "*",
            ratio_union_df.forward,
            ratio_union_df.backward,
            ratio_union_df.construction,
        )
        return ret_df

    def impute(df, link_col, marker, direction):
        if direction:
            # Forward imputation
            other_period_col = "previous_period"
        else:
            # Backward imputation
            other_period_col = "next_period"

        # Avoid having to care about the name of our link column by selecting
        # it as a fixed name here.
        working_df = df.select(
            col("ref"),
            col("period"),
            col(link_col).alias("link"),
            col("output"),
            col("marker"),
            col(other_period_col).alias("other_period"),
        ).persist()
        # Anything which isn't null is already imputed or a response and thus
        # can be imputed from.
        imputed_df = (
            working_df.filter(~col("output").isNull())
            .select("ref", "period", "link", "output", "marker")
            .persist()
        )
        # Any refs which have no values at all can't be imputed from so we
        # don't care about them here.
        for ref_val in imputed_df.select("ref").distinct().toLocalIterator():
            # Get all the periods and links for this ref where the output is
            # null since they're the periods we care about.
            ref_df = working_df.filter(
                (col("ref") == ref_val["ref"]) & (col("output").isNull())
            ).persist()
            # Make sure the periods are in the correct order so that we have
            # the other period we need to impute from where possible.
            for period_val in (
                ref_df.select("period", "other_period")
                .distinct()
                .sort("period", ascending=direction)
                .toLocalIterator()
            ):
                # Get the already present value for the other period if any.
                # At this point we don't care if it's a response, imputation
                # or construction only that it isn't null.
                other_value_df = (
                    imputed_df.filter(
                        (col("ref") == ref_val["ref"])
                        & (col("period") == period_val["other_period"])
                    )
                    .select(col("ref"), col("output").alias("other_output"))
                    .persist()
                )
                if other_value_df.count() == 0:
                    # We either have a gap or no value to impute from,
                    # either way nothing to do.
                    continue

                # Join the other value into this period so that we can
                # multiply by the link. We only need the ref column in both to
                # do the join.
                calculation_df = (
                    ref_df.filter(col("period") == period_val["period"])
                    .select("ref", "period", "link")
                    .join(other_value_df, "ref")
                    .select(
                        col("ref"),
                        col("period"),
                        col("link"),
                        (col("link") * col("other_output")).alias("output"),
                        lit(marker).alias("marker"),
                    )
                )
                # Store the imputed period for this ref in our imputed
                # dataframe so that it can be referenced by another period.
                imputed_df = imputed_df.union(calculation_df).persist()

        # We should now have an output column which is as fully populated as
        # this phase of imputation can manage. As such replace the existing
        # output column with our one. Same goes for the marker column.
        return df.drop("output", "marker").join(
            imputed_df.drop("link"), ["ref", "period"], "leftouter"
        )

    def forward_impute_from_response(df):
        return impute(df, "forward", MARKER_FORWARD_IMPUTE_FROM_RESPONSE, True)

    def backward_impute(df):
        return impute(df, "backward", MARKER_BACKWARD_IMPUTE, False)

    def construct_values(df):
        construction_df = df.filter(df.output.isNull()).select(
            "ref", "period", "aux", "construction", "previous_period"
        )
        prev_df = construction_df.select("ref", "previous_period").alias("prev")

        construction_df = construction_df.alias("cur")
        construction_df = construction_df.join(
            prev_df,
            [
                col("cur.ref") == col("prev.ref"),
                col("cur.period") != col("prev.previous_period"),
            ],
        ).select(
            col("cur.ref").alias("ref"),
            col("cur.period").alias("period"),
            (col("aux") * col("construction")).alias("constructed_output"),
            lit(MARKER_CONSTRUCTED).alias("constructed_marker"),
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

    def forward_impute_from_construction(df):
        return impute(df, "forward", MARKER_FORWARD_IMPUTE_FROM_CONSTRUCTION, True)

    # ----------

    return run(input_df)
