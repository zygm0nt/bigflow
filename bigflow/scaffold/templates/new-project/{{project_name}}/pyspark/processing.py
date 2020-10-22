{% skip_file_unless pyspark_job -%}

import random
import operator
import logging

import bigflow

import pyspark
import pyspark.sql


logger = logging.getLogger(__name__)


def run_pyspark_job(
    context: bigflow.JobContext,
    points,
    partitions,
):
    spark = pyspark.sql.SparkSession.builder.getOrCreate()
    logger.info("Calculate Pi, partitions=%s", partitions)

    def f(_):
        x = random.random() * 2 - 1
        y = random.random() * 2 - 1
        logger.debug("random point (%s, %s)", x, y)
        return 1 if x ** 2 + y ** 2 <= 1 else 0

    ticks = spark.sparkContext.parallelize(range(points), partitions)
    count = ticks.map(f).reduce(operator.add)
    pi = (4.0 * count / points)

    print(f"Pi is roughly {pi:1.9f}")
