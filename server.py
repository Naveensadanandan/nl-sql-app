import os
import requests
import json

try:
  from dotenv import load_dotenv
  load_dotenv()
except ImportError:
  pass

import time


def call_model():
  return requests.post(
    url="https://openrouter.ai/api/v1/chat/completions",
    headers={
      "Authorization": os.environ["OPENROUTER_API"],
      "Content-Type": "application/json",
    },
    data=json.dumps({
      "model": "xingchen-agi/xing4.0-29b-a4b:free",
      "messages": [
        {
          "role": "user",
          "content": "How many r's are in the word 'strawberry'?"
        }
      ],
      "reasoning": {"enabled": True}
    }),
    timeout=60,
  )


# Free models share an upstream pool and often return 429; retry with backoff.
for attempt in range(5):
  response = call_model()
  if response.status_code != 429:
    break
  time.sleep(2 ** attempt)

body = response.json()
if "error" in body:
  print("Error:", body["error"].get("message"), body["error"].get("code"))
else:
  message = body["choices"][0]["message"]
  print(message.get("content"))
  print("reasoning:", message.get("reasoning"))