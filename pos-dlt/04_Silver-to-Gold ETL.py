# Databricks notebook source
# MAGIC %md 
# MAGIC You may find this series of notebooks at https://github.com/databricks-industry-solutions/pos-dlt. For more information about this solution accelerator, visit https://www.databricks.com/solutions/accelerators/real-time-point-of-sale-analytics.

# COMMAND ----------

# MAGIC %md This notebook was developed to run as part of a DLT pipeline. Details on the scheduling of the DLT jobs are provided in the *POS 05* notebook.

# COMMAND ----------

# MAGIC %md **IMPORTANT NOTE** Do not attempt to interactively run the code in this notebook.  **This notebook must be scheduled as a DLT pipeline.**  If you attempt to run the notebook interactively, *e.g.* by running individual cells or clicking *Run All* from the top of the notebook you will encounter errors.  Job scheduling is addressed in the *POS 05* notebook.

# COMMAND ----------

# MAGIC %md 
# MAGIC The purpose of this notebook is to calculate the current-state inventory of products in various store locations leveraging data arriving in near real-time from point-of-sale systems.  Those data are exposed as Delta Live Table (DLT) objects in the *POS 3* notebook. 
# MAGIC 
# MAGIC This notebook should be scheduled to run while the *POS 02* notebook (which generates the simulated event data) runs on a separate cluster.  It also depends on the demo environment having been configured per the instructions in the *POS 01* notebook.

# COMMAND ----------

# MAGIC %md ## Step 1: Calculate Current Inventory
# MAGIC 
# MAGIC In the last notebook, we defined Delta Live Table (DLT) objects to capture inventory change event data as well as periodic snapshot data flowing from stores into our lakehouse environment.  Leveraging these data along with various reference data elements, we might issue a query as follows to calculate the current state of product inventory:
# MAGIC 
# MAGIC ```
# MAGIC SELECT 
# MAGIC   a.store_id,
# MAGIC   a.item_id,
# MAGIC   FIRST(a.quantity) as snapshot_quantity,  
# MAGIC   COALESCE(SUM(b.quantity),0) as change_quantity,
# MAGIC   FIRST(a.quantity) + COALESCE(SUM(b.quantity),0) as current_inventory,
# MAGIC   GREATEST(FIRST(a.date_time), MAX(b.date_time)) as date_time
# MAGIC FROM pos.latest_inventory_snapshot a  -- access latest snapshot
# MAGIC LEFT OUTER JOIN ( -- calculate inventory change with bopis corrections
# MAGIC   SELECT
# MAGIC     x.store_id,
# MAGIC     x.item_id,
# MAGIC     x.date_time,
# MAGIC     x.quantity
# MAGIC   FROM pos.inventory_change x
# MAGIC   INNER JOIN pos.store y
# MAGIC     ON x.store_id=y.store_id
# MAGIC   INNER JOIN pos.inventory_change_type z
# MAGIC     ON x.change_type_id=z.change_type_id
# MAGIC   WHERE NOT(y.name='online' AND z.change_type='bopis') -- exclude bopis records from online store
# MAGIC   ) b
# MAGIC   ON 
# MAGIC     a.store_id=b.store_id AND 
# MAGIC     a.item_id=b.item_id AND 
# MAGIC     a.date_time<=b.date_time
# MAGIC GROUP BY
# MAGIC   a.store_id,
# MAGIC   a.item_id
# MAGIC ORDER BY 
# MAGIC   date_time DESC
# MAGIC ```
# MAGIC 
# MAGIC The query is moderately complex as it joins inventory change data to the latest inventory counts taken for a given product at a store location to calculate a current state.  The joining of these two datasets is facilitated by a match on store_id and item_id where inventory change data flowing from the POS occurs on or after an inventory count for that product was performed.  Because it is possible no POS events have been recorded since an item was counted, this join is implemented as an outer join.
# MAGIC 
# MAGIC For an experienced Data Engineer reasonably comfortable with SQL, this is how a solution for current-state inventory calculations is intuitively defined. And this maps nicely to how we define the solution with Delta Live Tables (DLT):
# MAGIC 
# MAGIC **NOTE** For more information on the DLT SQL specification, please refer to [this document](https://docs.microsoft.com/en-us/azure/databricks/data-engineering/delta-live-tables/delta-live-tables-sql-ref).

# COMMAND ----------

# DBTITLE 1,Define Current Inventory Table (SQL)
import dlt
import pyspark.sql.functions as f

@dlt.table(
  name='inventory_current_python',
  comment='current inventory count for a product in a store location',
  table_properties={'quality':'gold'},
  spark_conf={'pipelines.trigger.interval': '5 minutes'}
  )
def inventory_current_python():
  
   # calculate inventory change with bopis corrections
   inventory_change_df = (
      dlt
       .read('inventory_change').alias('x')
       .join(
         dlt.read('store').alias('y'), 
         on='store_id'
         )
       .join(
         dlt.read('inventory_change_type').alias('z'), 
         on='change_type_id'
         )
       .filter(f.expr("NOT(y.name='online' AND z.change_type='bopis')"))
       .select('store_id','item_id','date_time','quantity')
       )
   
   # calculate current inventory
   inventory_current_df = (
      dlt
         .read('latest_inventory_snapshot').alias('a')
         .join(
           inventory_change_df.alias('b'), 
           on=f.expr('''
             a.store_id=b.store_id AND 
             a.item_id=b.item_id AND 
             a.date_time<=b.date_time
             '''), 
           how='leftouter'
           )
         .groupBy('a.store_id','a.item_id')
           .agg(
               f.first('a.quantity').alias('snapshot_quantity'),
               f.sum('b.quantity').alias('change_quantity'),
               f.first('a.date_time').alias('snapshot_datetime'),
               f.max('b.date_time').alias('change_datetime')
               )
         .withColumn('change_quantity', f.coalesce('change_quantity', f.lit(0)))
         .withColumn('current_quantity', f.expr('snapshot_quantity + change_quantity'))
         .withColumn('date_time', f.expr('GREATEST(snapshot_datetime, change_datetime)'))
         .drop('snapshot_datetime','change_datetime')
         .withColumn("inventory_type", f.when(f.rand() > 0.9, "low quantity item").when(f.rand() > 0.5, "medium quantity item").otherwise("high quantity item"))
         .withColumn(
         "stock_status", f.when(f.col("inventory_type") == "low quantity item", f.when(f.col("current_quantity") < 3, "low").when(f.col("current_quantity") <= 5, "medium quantity item").otherwise("high"))
           .when(f.col("inventory_type") == "medium quantity item", f.when(f.col("current_quantity") < 10, "low").when(f.col("current_quantity") < 20, "medium").otherwise("high"))
           .otherwise(f.when(f.col("current_quantity") < 50, "low").when(f.col("current_quantity") < 70, "medium").otherwise("high")))
         .orderBy('current_quantity', "inventory_type")
         )
   
   return inventory_current_df

# COMMAND ----------

@dlt.table(
    name="best_supplier",
    comment="best suppliers for low stock items",
    table_properties={"quality": "gold"},
    spark_conf={"pipelines.trigger.interval": "5 minutes"}
)
def best_supplier():

    suppliers_df = (
        dlt.read("inventory_current_python").alias('a')
          .join(
              dlt.read("suppliers").alias('b'), on="item_id", how="inner"
          )
        .filter(f.col("stock_status") == "low")
        .withColumn("top_supplier", f.when(f.greatest("supplier1", "supplier2", "supplier3") == f.col("supplier1"), "supplier1")
          .when(f.greatest("supplier1", "supplier2", "supplier3") == f.col("supplier2"), "supplier2").otherwise("supplier3"))
        .select("name", "inventory_type", "current_quantity", "top_supplier", "date_time")
    )

    return suppliers_df

# COMMAND ----------

# MAGIC %md Of course, we could have used Python (as we did in the *POS 03* notebook) to define this object.  If we did, our implementation might look something like this:
# MAGIC 
# MAGIC ```
# MAGIC 
# MAGIC @dlt.table(
# MAGIC   name='inventory_current_python',
# MAGIC   comment='current inventory count for a product in a store location',
# MAGIC   table_properties={'quality':'gold'},
# MAGIC   spark_conf={'pipelines.trigger.interval': '5 minutes'}
# MAGIC   )
# MAGIC  def inventory_current_python():
# MAGIC   
# MAGIC    # calculate inventory change with bopis corrections
# MAGIC    inventory_change_df = (
# MAGIC       dlt
# MAGIC        .read('inventory_change').alias('x')
# MAGIC        .join(
# MAGIC          dlt.read('store').alias('y'), 
# MAGIC          on='store_id'
# MAGIC          )
# MAGIC        .join(
# MAGIC          dlt.read('inventory_change_type').alias('z'), 
# MAGIC          on='change_type_id'
# MAGIC          )
# MAGIC        .filter(f.expr("NOT(y.name='online' AND z.change_type='bopis')"))
# MAGIC        .select('store_id','item_id','date_time','quantity')
# MAGIC        )
# MAGIC    
# MAGIC    # calculate current inventory
# MAGIC    inventory_current_df = (
# MAGIC       dlt
# MAGIC          .read('latest_inventory_snapshot').alias('a')
# MAGIC          .join(
# MAGIC            inventory_change_df.alias('b'), 
# MAGIC            on=f.expr('''
# MAGIC              a.store_id=b.store_id AND 
# MAGIC              a.item_id=b.item_id AND 
# MAGIC              a.date_time<=b.date_time
# MAGIC              '''), 
# MAGIC            how='leftouter'
# MAGIC            )
# MAGIC          .groupBy('a.store_id','a.item_id')
# MAGIC            .agg(
# MAGIC                first('a.quantity').alias('snapshot_quantity'),
# MAGIC                sum('b.quantity').alias('change_quantity'),
# MAGIC                first('a.date_time').alias('snapshot_datetime'),
# MAGIC                max('b.date_time').alias('change_datetime')
# MAGIC                )
# MAGIC          .withColumn('change_quantity', f.coalesce('change_quantity', f.lit(0)))
# MAGIC          .withColumn('current_quantity', f.expr('snapshot_quantity + change_quantity'))
# MAGIC          .withColumn('date_time', f.expr('GREATEST(snapshot_datetime, change_datetime)'))
# MAGIC          .drop('snapshot_datetime','change_datetime')
# MAGIC          .orderBy('current_quantity')
# MAGIC          )
# MAGIC    
# MAGIC    return inventory_current_df
# MAGIC ```

# COMMAND ----------

# MAGIC %md It's important to note that the current inventory table is implemented using a 5-minute recalculation. While DLT supports near real-time streaming, the business objectives associated with the calculation of near current state inventories do not require up to the second precision.  Instead, 5-, 10- and 15-minute latencies are often preferred to give the data some stability and to reduce the computational requirements associated with keeping current.  From a business perspective, responses to diminished inventories are often triggered when values fall below a threshold that's well-above the point of full depletion (as lead times for restocking may be measured in hours, days or even weeks). With that in mind, the 5-minute interval used here exceeds the requirements of many retailers.

# COMMAND ----------

# MAGIC %md
# MAGIC 
# MAGIC &copy; 2022 Databricks, Inc. All rights reserved. The source in this notebook is provided subject to the [Databricks License](https://databricks.com/db-license-source).  All included or referenced third party libraries are subject to the licenses set forth below.
# MAGIC 
# MAGIC | library                                | description             | license    | source                                              |
# MAGIC |----------------------------------------|-------------------------|------------|-----------------------------------------------------|
# MAGIC | azure-iot-device                                     | Microsoft Azure IoT Device Library | MIT    | https://pypi.org/project/azure-iot-device/                       |
# MAGIC | azure-storage-blob                                | Microsoft Azure Blob Storage Client Library for Python| MIT        | https://pypi.org/project/azure-storage-blob/      |
