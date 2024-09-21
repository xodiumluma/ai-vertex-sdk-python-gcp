import os
import import_data_text_entity_extraction_sample
import pytest

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
LOCATION = "us-central1"
GCS_URI = "gs://ucaip-test-us-central1/dataset/ucaip_ten_dataset.jsonl"
METADATA_SCHEMA_URI = (
  "gs://google-cloud-aiplatform/schema/dataset/metadata/text_1.0.0.yaml"
)
