import os
from uuid import uuid4

import create_dataset_video_sample
import pytest

import helpers

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
VIDEO_METADATA_SCHEMA_URI = (
  "gs://google-cloud-aiplatform/schema/dataset/metadata/video_1.0.0.yaml"
)

@pytest.fixture(scope="function", autouse=True)
def teardown(tearown_dataset):
  yield

@pytest.mark.skip(reason="https://github.com/googleapis/java-aiplatform/issues/420")
def test_ucaip_generated_create_dataset_video_sample_vision(capsys, shared_state):
  create_dataset_video_sample.create_dataset_video_sample(
    display_name=f"temp_create_dataset_test_{uuid4()}", project=PROJECT_ID
  )
  out, _ = capsys.readouterr()
  assert "create_dataset_response" in out

  shared_state["dataset_name"] = helpers.get_name(out)
  