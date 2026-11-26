# Authentication guide

To call the API, send your key in the header:

```http
GET /v1/items HTTP/1.1
Host: api.example.invalid
X-API-Key: <your-api-key>
Authorization: Bearer <token>
```

In Python:

```python
import os
import requests

api_key = os.environ["API_KEY"]
resp = requests.get(url, headers={"X-API-Key": api_key})
```

In a config file:

```yaml
api:
  key: ${API_KEY}
  token_ttl: 3600
```

Passwords: set `password` to a value of at least 12 characters in `.env` (never commit it).
The `password: |` form of YAML is not supported for this setting; use one line.

```json
{"name": "API_KEY", "value": "<set me>"}
```

If the key leaks, rotate it in the console and update the secret in your CI settings.
# Authentication guide

To call the API, send your key in the header:

```http
GET /v1/items HTTP/1.1
Host: api.example.invalid
X-API-Key: <your-api-key>
Authorization: Bearer <token>
```

In Python:

```python
import os
import requests

api_key = os.environ["API_KEY"]
resp = requests.get(url, headers={"X-API-Key": api_key})
```

In a config file:

```yaml
api:
  key: ${API_KEY}
  token_ttl: 3600
```

Passwords: set `password` to a value of at least 12 characters in `.env` (never commit it).
The `password: |` form of YAML is not supported for this setting; use one line.

```json
{"name": "API_KEY", "value": "<set me>"}
```

If the key leaks, rotate it in the console and update the secret in your CI settings.
# Authentication guide

To call the API, send your key in the header:

```http
GET /v1/items HTTP/1.1
Host: api.example.invalid
X-API-Key: <your-api-key>
Authorization: Bearer <token>
```

In Python:

```python
import os
import requests

api_key = os.environ["API_KEY"]
resp = requests.get(url, headers={"X-API-Key": api_key})
```

In a config file:

```yaml
api:
  key: ${API_KEY}
  token_ttl: 3600
```

Passwords: set `password` to a value of at least 12 characters in `.env` (never commit it).
The `password: |` form of YAML is not supported for this setting; use one line.

```json
{"name": "API_KEY", "value": "<set me>"}
```

If the key leaks, rotate it in the console and update the secret in your CI settings.
