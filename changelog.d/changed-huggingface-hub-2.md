- **Breaking:** The engine now requires `huggingface-hub` 2.x, which raises `httpx2` errors instead of
  `httpx` errors. Node libraries that catch `httpx` exceptions from Hugging Face calls must catch the
  `httpx2` equivalents.
