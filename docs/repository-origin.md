# Repository origin

This standalone repository was extracted from
[lukeshensf/ravn](https://github.com/lukeshensf/ravn), branch
`codex/headless-ravn-mvp`, at commit
`d184e06235193531ff64e727e8fb84d213ab03ca`.

## Included source

- `broker/` was promoted to the repository root.
- The broker CI workflow and four broker design documents were retained.
- The original Apache-2.0 license and bundled third-party notices were retained.

Both broker commits retain their original authors, timestamps, messages, and
parent order. Filtering paths changes Git commit IDs:

| Original commit | Extracted commit | Change |
|---|---|---|
| `646ac28e83984279454cf709e150401a45feb19c` | `eedfe42fe8cc94cd4ff59029c4450178446c6e11` | Headless broker, OAuth, console, and Support Desk |
| `d184e06235193531ff64e727e8fb84d213ab03ca` | `84892f5109541630fa2c9a72cb09b533441285bf` | Gmail and reviewed write support |

A subsequent standalone-repository commit updates documentation, ignore rules,
package license metadata, and CI paths. It does not change broker behavior.

## Starting a new deployment

Runtime databases, private keys, OAuth credentials, local environments, and
dependency caches are not part of this repository. Follow the [quick start](../README.md)
for a simulated demo or [onboarding](onboarding.md) for a new live deployment.
An existing deployment's encrypted database requires its matching configuration
and master key; copying the source code does not transfer account connections.
