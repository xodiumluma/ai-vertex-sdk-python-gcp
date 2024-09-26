import os
import import_data_text_sentiment_analysis_sample
import pytest

PROJECT_ID = os.getenv("BUILD_SPECIFIC_GCLOUD_PROJECT")
LOCATION = "us-central1"
GCS_SOURCE = (
  "gs://cloud-samples-data-us-central1/ai-platform/natural_language/"
  "sentiment_analysis/dataset_ucaip_tst_dataset_10.csv"
)

@pytest.fixture(scope="function", autouse=True)
def setup(create_dataset):
  create_dataset(PROJECT_ID, LOCATION, METADATA_SCHEMA_URI)
  yield

def test_ucaip_generated_import_data_text_sentiment_analysis_sample(
  capsys, shared_state
):
  dataset_id = shared_state["dataset_name"].split("/")[-1]

  import_data_text_sentiment_analysis_sample.import_data_text_sentiment_analysis_sample(
    gcs_source_uri=GCS_SOURCE,
    project=PROJECT_ID,
    dataset_id=dataset_id
  )

  out, _ = capsys.readouterr()

  assert "import_data_response" in out
