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
            df = stage(df).localCheckpoint(eager=False)
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
        if "forward" in df.columns:
            return df

        # Since we're going to join on to the main df at the end filtering for
        # nulls won't cause us to lose strata as they'll just be filled with
        # default ratios.
        filtered_df = df.filter(~df.output.isNull()).select(
            "ref",
            "period",
            "strata",
            "output",
            "aux",
            "previous_period",
            "next_period",
        )

        # Put the values from the current and previous periods for a
        # contributor on the same row. Then calculate the sum for both
        # for all contributors in a period as the values now line up.
        working_df = filtered_df.alias("current")
        working_df = working_df.join(
            filtered_df.select("ref", "period", "output").alias("prev"),
            [
                col("current.ref") == col("prev.ref"),
                col("current.previous_period") == col("prev.period"),
            ],
            "leftouter",
        ).select(
            col("current.strata").alias("strata"),
            col("current.period").alias("period"),
            when(~col("prev.output").isNull(), col("current.output")).alias("output"),
            col("current.aux").alias("aux"),
            col("prev.output").alias("other_output"),
            col("current.output").alias("output_for_construction"),
        )
        working_df = working_df.groupBy("period", "strata").agg(
            {
                "output": "sum",
                "other_output": "sum",
                "aux": "sum",
                "output_for_construction": "sum",
            }
        )
        # Calculate the forward ratio for every period using 1 in the
        # case of a 0 denominator. We also calculate construction
        # links at the same time for efficiency reasons. This shares
        # the same behaviour of defaulting to a 1 in the case of a 0
        # denominator.
        forward_df = (
            working_df.withColumn(
                "forward",
                col("sum(output)")
                / when(col("sum(other_output)") == 0, lit(1.0)).otherwise(
                    col("sum(other_output)")
                ),
            )
            .withColumn(
                "construction",
                col("sum(output_for_construction)")
                / when(col("sum(aux)") == 0, lit(1.0)).otherwise(col("sum(aux)")),
            )
            .fillna(1.0, ["forward", "construction"])
            .join(
                filtered_df.select("period", "strata", "next_period").distinct(),
                ["period", "strata"],
            )
        )

        # Calculate backward ratio as 1/forward for the next period for each
        # strata.
        strata_ratio_df = (
            forward_df.join(
                forward_df.select(
                    col("period").alias("other_period"),
                    col("forward").alias("next_forward"),
                    col("strata").alias("other_strata"),
                ),
                [
                    col("next_period") == col("other_period"),
                    col("strata") == col("other_strata"),
                ],
                "leftouter",
            )
            .select(
                col("period"),
                col("strata"),
                col("forward"),
                (lit(1.0) / col("next_forward")).alias("backward"),
                col("construction"),
            )
            .fillna(1.0, "backward")
        )

        # Join the strata ratios onto the input such that each contributor has
        # a set of ratios.
        ret_df = df.join(strata_ratio_df, ["period", "strata"]).select(
            "*",
            strata_ratio_df.forward,
            strata_ratio_df.backward,
            strata_ratio_df.construction,
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
        ref_df = imputed_df.select("ref").distinct().persist()
        filtered_df = (
            working_df.filter(col("output").isNull()).join(ref_df, "ref").persist()
        )
        while True:
            other_df = imputed_df.select("ref", "period", "output").alias("other")
            calculation_df = (
                filtered_df.alias("filtered")
                .join(
                    other_df,
                    [
                        col("filtered.other_period") == col("other.period"),
                        col("filtered.ref") == col("other.ref"),
                    ],
                )
                .select(
                    col("filtered.ref").alias("ref"),
                    col("filtered.period").alias("period"),
                    col("filtered.link").alias("link"),
                    (col("filtered.link") * col("other.output")).alias("output"),
                    lit(marker).alias("marker"),
                )
                .persist()
            )
            # If we've imputed nothing then we've got as far as we can get for
            # this phase.
            if calculation_df.count() == 0:
                break

            # Store this set of imputed values in our main set for the next
            # iteration.
            imputed_df = imputed_df.union(calculation_df).persist()
            # Remove the newly imputed rows from our filtered set.
            filtered_df = filtered_df.join(
                calculation_df.select("ref", "period"), ["ref", "period"], "leftanti"
            ).persist()

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
        other_df = construction_df.select("ref", "period").alias("other")
        construction_df = construction_df.alias("construction")
        construction_df = construction_df.join(
            other_df,
            [
                col("construction.ref") == col("other.ref"),
                col("construction.previous_period") == col("other.period"),
            ],
            "leftanti",
        ).select(
            col("construction.ref").alias("ref"),
            col("construction.period").alias("period"),
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