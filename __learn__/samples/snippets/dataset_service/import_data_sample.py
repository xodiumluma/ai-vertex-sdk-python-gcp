# [START aiplatform_import_data_sample]
from google.cloud import aiplatform

def import_data_sample(
  project: str,
  dataset_id: str,
  gcs_source_uri: str,
  import_schema_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central-aiplatform.googleapis.com",
  timeout: int = 1800,
):
  # regional API endpoints
  


# [END aiplatform_import_data_sample]