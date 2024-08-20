import os
from uuid import uuid4

import create_dataset_tabular_bigquery_sample
import pytest

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
BIGQUERY_URI = "bq://ucaip-sample-tests.table_test.all_bq_types"

@pytest.fixture(scope="function", autouse=True)
def teardown(teardown_dataset):
  yield

def test_ucaip_generated_create_dataset_tabular_bigquery(capsys, shared_state):
  create_dataset_tabular_bigquery_sample.create_dataset_tabular_bigquery_sample(
    display_name=f"temp_create_dataset_test_{uuid_64()}",
    bigquery_uri=BIGQUERY_URI,
    project=PROJECT_ID
  )
  out, _ = capsys.readouterr()
  assert "create_dataset_response" in out

  shared_state["dataset_name"] = helpers.get_name(out)