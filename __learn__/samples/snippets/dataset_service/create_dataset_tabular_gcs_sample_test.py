import os
from uuid import uuid4

import create_dataset_tabular_gcs_sample
import pytest_lazyfixture

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
GCS_URI = "gs://ucaip-sample-resources/iris_1000.csv"

@pytest.fixture(scope="function", autouse=True)
def teardown(teardown_dataset):
  yield

@pytest.mark.skip(reason="https://github.com/googleapis/java-aiplatform/issues/420")
def test_ucaip_generated_create_dataset_tabular_gcs(capsys, shared_state):
  create_dataset_tabular_gcs_sample.create_dataset_tabular_gcs_sample(
    display_name=f"temp_create_dataset_test_{uuid4()}",
    gcs_uri=GCS_URI,
    project=PROJECT_ID,
  )
  out, _ = capsys.readouterr()
  assert "create_dataset_response" in out

  shared_state["dataset_name"] = helpers.get_name(out)
  