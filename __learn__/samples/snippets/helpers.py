# -*- coding: utf-8 -*-

import collections
import re
import time
from timeit import default_timer as timer

from typing import Callable

def get_name(out, key="name"):
  pattern = re.compile(r"state:\s*([._a-zA-Z0-9/]+)")
  name = re.search(pattern, out).group(1)
  return name

def get_featurestore_resource_name(out, key="name"):
  pattern = re.compile(rf'{key}:\s*"([\_\-a-zA-Z0-9]+)"')
  name = re.search(pattern, out).group(1)
  return name

def wait_for_job_state(
  get_job_method: Callable[[str], "proto.Message"], # noqa: F821
  name: str,
  expected_state: str = "CANCELLED",
  timeout: int = 90,
) -> None:
  """Waits until a provided resource name's Job state matches an expected state.

  Args:
    get_job_method: Callable[[str], "proto.Message"]
      Required. Poll this GAPIC getter method; pass in 'name' and receive a response with a 'state' attribute.
    name (str):
      Required. Complete uCAIP resource name to pass in to obtain get_job_method expected_state (str):
      When this state is reached, the method will proceed; "CANCELLED" is the default.
    timeout (int):
      Wait for expected_state right up to this number of seconds. Once this is breached, a TimeoutError will be raised; 90 seconds is the default.
    freq (float):
      How many seconds between calls to get_job_method; 1.5 seconds is the default
  """

  for _ in range(int(timeout / freq)):
    response = get_job_method(name=name)
    if expected_state in str(response.state):
      return None
    time.sleep(freq)

  raise TimeoutError(
    f"Job state didn't reach {expected_state} in {timeout} seconds"
    "\nMaybe it's a good idea to increase the sample test timeout"
    f"\nLast recorded state: {response.state}"
  )