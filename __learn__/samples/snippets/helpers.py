import collections
import re
import time
from timeit import default_timer as timer

from typing import Callable

def get_name(out, key="name"):
  pattern = re.compile(rf'{key}:\s*"([\-a-zA-Z0-9/]+)"')
  name = re.search(pattern, out).group(1)

  return