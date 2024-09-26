import os
import import_data_text_sentiment_analysis_sample
import pytest

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
LOCATION = "us-central1"
GCS_SOURCE = (
  "gs://cloud-samples-data-us-central1/ai-platform/natural_language/"
  "sentiment_analysis/dataset_ucaip_tst_dataset_10.csv"

@pytest.fixture(scope="function", autouse=True)
def setup(create_dataset):
  create_dataset(PROJECT_ID, LOCATION, METADATA_SCHEMA_URI)
  yield