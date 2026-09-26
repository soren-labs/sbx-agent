"""Modal deployment of the Sorenforge GitHub integration broker (SOR-220).

Deploy: ``modal deploy broker/modal_deploy.py`` (Modal credentials required).

The broker is the ONLY component that holds the public SBX App's private
key. It arrives exclusively via the Modal Secret ``sbx-github-broker``
(env ``SBX_BROKER_APP_ID`` / ``SBX_BROKER_APP_SLUG`` /
``SBX_BROKER_APP_PRIVATE_KEY`` and optional ``SBX_BROKER_STATE_SECRET``) —
never committed, never in the image, never sent to a deployment.

Durable state (signed states in flight, one-time claim codes, per-
installation bindings holding only credential *hashes*) lives in the
``modal.Dict`` named by ``SBX_BROKER_STORE_DICT`` so container churn never
drops a bound installation.
"""

import modal

app = modal.App("sbx-github-broker")

# Only the two packages the broker serves: ``broker`` (HTTP surface +
# service) and ``control`` (the shared SOR-177 GitHub App client it reuses).
image = (
    modal.Image.debian_slim()
    .pip_install("fastapi[standard]", "httpx", "PyJWT[crypto]", "pydantic")
    .add_local_python_source("broker", "control")
)


@app.function(
    image=image,
    secrets=[modal.Secret.from_name("sbx-github-broker")],
    env={"SBX_BROKER_STORE_DICT": "sbx-github-broker-store"},
    min_containers=1,
)
@modal.asgi_app()
def web():
    from broker.app import create_app

    return create_app()
