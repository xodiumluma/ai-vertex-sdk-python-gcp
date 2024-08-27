# [START aiplatform_create_dataset_tabular_gcs_sample]
from google.cloud import aiplatform
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

def create_dataset_tabular_gcs_sample(
  project: str,
  display_name: str,
  gcs_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform-googleapis.com",
  timeout: int = 300,
):
  # provide regional API endpoints
  client_options = {"api_endpoint": api_endpoint}
  # initialise reusable client
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  metadata_dict = {"input_config": {"gcs_source": {"uri": [gcs_uri]}}}
  metadata = json_format.ParseDict(metadata_dict, Value())

  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": "gs://google-cloud-platform/schema/dataset/metadata/tabular-1.0.0.yaml",
    "metadata": metadata,
  }

  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("Lengthy operation:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("create_dataset_response:", create_dataset_response)

# [END aiplatform_create_dataset_tabular_gcs_sample]