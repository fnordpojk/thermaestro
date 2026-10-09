# Contributing to Thermaestro

Thank you for helping. Everyone taking part follows the [code of conduct](CODE_OF_CONDUCT.md).

## Reporting a bug

Open an issue, and say:

- what you did, what you expected, and what happened;
- the commit you run (`main` is the only supported version) and how you run it (Docker or from source);
- the log lines around the problem. Look them over first: the log holds no secrets, but it can hold your network's addresses.

For a problem with a heat pump, or a model Thermaestro doesn't know yet, add the report and capture from [the read-only probe](docs/probe.md). It writes nothing to the pump.

A security problem goes to [the security policy](SECURITY.md), never to a public issue.

## Proposing a change

For anything bigger than a small fix, open an issue first to talk it through. That saves work on a change that doesn't fit.

Then:

1. Fork the repository and make a branch from `main`.
2. Set up as [the developer guide](docs/development.md) says, and make your change with tests.
3. Run the checks CI runs (they're listed in the developer guide). A pull request is merged only with CI green.
4. Sign off every commit (below), and open a pull request that says what it changes and why.

Keep to how the code already reads: American English, short commit messages (a subject line and at most a few lines on why), and secrets never logged.

## Signing off: the Developer Certificate of Origin

Each commit must be signed off, which certifies that you wrote it or otherwise have the right to contribute it under the project's license. That is the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Sign off with:

```
git commit -s
```

which adds a line with your name and email:

```
Signed-off-by: Your Name <you@example.com>
```

Use your real name. To sign off commits you've already made on your branch, `git rebase --signoff main`.

## License

Contributions are licensed as the files they change: AGPL-3.0-or-later, and the test vectors in `testvectors/` also under the MIT license.
