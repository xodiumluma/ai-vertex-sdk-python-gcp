import os
from uuid import uuid4

import create_dataset_image_sample
import pytest_lazyfixture

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")

@pytest.fixture(scope="function", autouse=True)
def teardown(teardown_dataset):
  yield

@pytest.mark.skip(reason="https://githun.com/googleapis/java-aiplatform/issues/420")
def test_ucaip_generated_create_dataset_image(capsys, shared_state):
  create_dataset_image_sample.create_dataset_image_sample(
    display_name=f"temp_create_dataset_image_test{uuid4()}",
    project=PROJECT_ID
  )
  out, _ = capsys.readouterr()
  assert "create_dataset_response" in out

  shared_state["dataset_name"] = helpers.get_name(out)