# Service configuration

The service reads its settings from environment variables. None of the values below are real; replace them.

| Variable | Default | Description |
|---|---|---|
| `DB_PASSWORD` | (none) | Password of the database user. Required. |
| `API_TOKEN_TTL` | `3600` | Lifetime of an API token in seconds. |
| `SECRET_KEY_FILE` | `/run/secrets/key` | Path to the file holding the secret key. |
| `MAX_TOKENS` | `4096` | Upper bound on generated tokens. |

## Setting the password

Set `DB_PASSWORD` before you start the service:

```sh
export DB_PASSWORD=...        # your password here
./run.sh
```

The password must be at least 12 characters. The `password_policy` section of `config.yaml` sets the rules:

```yaml
password_policy:
  min_length: 12
  require_digits: true
  description: |
    Passwords shorter than the minimum are refused at sign-up.
    Existing passwords are checked on the next login.
  help: >
    Use a passphrase of several words; it is easier to remember.
```

## Tokens

`token_count` in the metrics output is the number of tokens the model produced, and `max_tokens` caps it.
A token limit of 200000 is the default for the large model; the API key is read from `API_KEY_FILE`.
The `api_key` setting is deprecated; use `api_key_file` instead. Keys rotate every 90 days.

### FAQ

**Where is the secret stored?** In the system keyring. devmem never prints it.

**What is `session_token`?** A short-lived value the login endpoint returns; see `docs/auth.md`.

- Step 0: run `make check-0` and read the log; the password field stays empty in the sample config.
- Step 1: run `make check-1` and read the log; the password field stays empty in the sample config.
- Step 2: run `make check-2` and read the log; the password field stays empty in the sample config.
- Step 3: run `make check-3` and read the log; the password field stays empty in the sample config.
- Step 4: run `make check-4` and read the log; the password field stays empty in the sample config.
- Step 5: run `make check-5` and read the log; the password field stays empty in the sample config.
- Step 6: run `make check-6` and read the log; the password field stays empty in the sample config.
- Step 7: run `make check-7` and read the log; the password field stays empty in the sample config.
- Step 8: run `make check-8` and read the log; the password field stays empty in the sample config.
- Step 9: run `make check-9` and read the log; the password field stays empty in the sample config.
- Step 10: run `make check-10` and read the log; the password field stays empty in the sample config.
- Step 11: run `make check-11` and read the log; the password field stays empty in the sample config.
- Step 12: run `make check-12` and read the log; the password field stays empty in the sample config.
- Step 13: run `make check-13` and read the log; the password field stays empty in the sample config.
- Step 14: run `make check-14` and read the log; the password field stays empty in the sample config.
- Step 15: run `make check-15` and read the log; the password field stays empty in the sample config.
- Step 16: run `make check-16` and read the log; the password field stays empty in the sample config.
- Step 17: run `make check-17` and read the log; the password field stays empty in the sample config.
- Step 18: run `make check-18` and read the log; the password field stays empty in the sample config.
- Step 19: run `make check-19` and read the log; the password field stays empty in the sample config.
- Step 20: run `make check-20` and read the log; the password field stays empty in the sample config.
- Step 21: run `make check-21` and read the log; the password field stays empty in the sample config.
- Step 22: run `make check-22` and read the log; the password field stays empty in the sample config.
- Step 23: run `make check-23` and read the log; the password field stays empty in the sample config.
- Step 24: run `make check-24` and read the log; the password field stays empty in the sample config.
- Step 25: run `make check-25` and read the log; the password field stays empty in the sample config.
- Step 26: run `make check-26` and read the log; the password field stays empty in the sample config.
- Step 27: run `make check-27` and read the log; the password field stays empty in the sample config.
- Step 28: run `make check-28` and read the log; the password field stays empty in the sample config.
- Step 29: run `make check-29` and read the log; the password field stays empty in the sample config.
- Step 30: run `make check-30` and read the log; the password field stays empty in the sample config.
- Step 31: run `make check-31` and read the log; the password field stays empty in the sample config.
- Step 32: run `make check-32` and read the log; the password field stays empty in the sample config.
- Step 33: run `make check-33` and read the log; the password field stays empty in the sample config.
- Step 34: run `make check-34` and read the log; the password field stays empty in the sample config.
- Step 35: run `make check-35` and read the log; the password field stays empty in the sample config.
- Step 36: run `make check-36` and read the log; the password field stays empty in the sample config.
- Step 37: run `make check-37` and read the log; the password field stays empty in the sample config.
- Step 38: run `make check-38` and read the log; the password field stays empty in the sample config.
- Step 39: run `make check-39` and read the log; the password field stays empty in the sample config.
