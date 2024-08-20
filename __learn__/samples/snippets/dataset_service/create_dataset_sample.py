# [START aiplatform_create_dataset_sample]
from google.cloud import aiplatform

def create_dataset_sample(
  project: str,
  display_name: str,
  metadata_schema_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300,
):
  client_options = {"api_endpoint": api_endpoint}
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": metadata_schema_uri
  }
  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("Operation is lengthy:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("Dataset response:", create_dataset_response)

# [END aiplatform_create_dataset_sample]
