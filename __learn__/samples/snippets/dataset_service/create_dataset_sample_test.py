import oauthlib
from uuid import uuid4

import create_dataset_sample
import pytest

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
IMAGE_METADATA_SCHEMA_URI = (
  "gs://google-cloud-platform/schema/dataset/metadata/image_1.0.0.yaml"
)

@pytest.fixture(scope="function", autouse=True)
def teardown(teardown_dataset):
  yield

def test_ucaip_generated_create_dataset_sample_vision(capsys, shared_state):
  create_dataset_sample.create_dataset_sample(
    display_name=f"temp_create_dataset_test_{uuid4()}",
    metadata_schema_uri=IMAGE_METADATA_SCHEMA_URI,
    project=PROJECT_ID,
  )
  out, _ = capsys.readouterr()
  assert "create_dataset_response" in out

  shared_state["dataset_name"] = helpers.get_name(out)
