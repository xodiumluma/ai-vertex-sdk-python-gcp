# [START aiplatform_create_dataset_tabular_bigquery_sample]
from google.cloud import aiplatform
from google.protobuf import json_format
from google.protobuf.struct_pb2 import Value

def create_dataset_tabular_bigquery_sample(
  project: str,
  display_name: str,
  bigquery_uri: str,
  location: str = "us-central1",
  api_endpoint: str = "us-central1-aiplatform.googleapis.com",
  timeout: int = 300,
):
  # regional endpoints needed
  client_options = {"api_endpoint": api_endpoint}
  # instantiate client - it's a reusable singleton
  client = aiplatform.gapic.DatasetServiceClient(client_options=client_options)
  metadata_dict = {"input_config": {"bigquery_source": {"uri": bigquery_uri}}}
  metadata = json_format.ParseDict(metadata_dict, Value())

  dataset = {
    "display_name": display_name,
    "metadata_schema_uri": "gs://google-cloud-aiplatform/schema/dataset/metadata/tabular_1.0.0.yaml",
    "metadata": metadata,
  }
  parent = f"projects/{project}/locations/{location}"
  response = client.create_dataset(parent=parent, dataset=dataset)
  print("Lengthy process:", response.operation.name)
  create_dataset_response = response.result(timeout=timeout)
  print("create_dataset_response:", create_dataset_response)

# [END aiplatform_create_dataset_tabular_bigquery_sample]
