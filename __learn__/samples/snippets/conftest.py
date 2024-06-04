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