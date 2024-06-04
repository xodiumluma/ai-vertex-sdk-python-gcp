import os
from uuid import uuid4

from google.api_core import exceptions

from google.cloud import aiplatform, aiplatform_v1beta1
from google.cloud import bigquery
from google.cloud import storage

import pytest
import helpers

@pytest.fixture()
def shared_state():
  state = {}
  yield state

@pytest.fixture
def storage_client():
  storage_client = storage.Client()
  return storage_client

@pytest.fixture()
def job_client():
  job_client = aiplatform.gapic.JobServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  return job_client

@pytest.fixture()
def data_labeling_job_client():
  endpoint = os.getenv("DATA_LABELING_API_ENDPOINT")
  return aiplatform.gapic.JobServiceClient(
    client_options={"api_endpoint": endpoint}
  )

@pytest.fixture
def pipeline_client():
  client = aiplatform.gapic.PipelineServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  return pipeline_client

@pytest.fixture
def model_client():
  client = aiplatform.gapic.ModelServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield model_client

@pytest.fixture
def endpoint_client():
  client = aiplatform.gapic.EndpointServiceClient(
    client_options={"api_endpoint": "us-central1-aiplatform.googleapis.com"}
  )
  yield client