# [START aiplatform_import_data_video_classification_sample]
from google.cloud import aiplatform

def import_data_video_classification_sample(
  project: str,
  dataset_id: str,
  gcs_source_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 1800,
):
# [END aiplaform_import_data_video_classification_sample]