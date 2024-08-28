# [START aiplatform_create_dataset_text_sample]
from google.cloud import aiplatform

def create_dataset_text_sample(
  project: str,
  display_name: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300,
):
  # we need regional API endpoints
  client_options = {"api_endpoint": api_endpoint}
  # instantiate reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": "gs://google-cloud-aiplatform/schema/dataset/metadata/text_1.0.0.yaml",
  }
  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("Lengthy operation:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("create_dataset_response:", create_dataset_response)

# [END aiplatform_create_dataset_text_sample]