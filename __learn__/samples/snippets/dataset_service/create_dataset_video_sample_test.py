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
