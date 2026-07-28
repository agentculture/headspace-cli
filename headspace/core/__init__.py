"""Backend-neutral core of headspace: lifecycle, policy, state, artifacts.

Nothing in this package may import a provider or the docker SDK — the provider
Protocol lives in :mod:`headspace.providers` and depends on this package, never
the other way around. That direction is what keeps the result contract
backend-neutral (spec claim c4).
"""
