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