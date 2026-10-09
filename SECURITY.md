# Security policy

## Supported versions

Thermaestro has no release yet. Only the `main` branch is supported, and fixes go there.

## Reporting a vulnerability

Please report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**, or go straight to https://github.com/fnordpojk/thermaestro/security/advisories/new.

Please don't open a public issue or pull request for a vulnerability.

A useful report says:

- what is affected: the core, a plugin, the web interface or API, or the gateway;
- how to reproduce it, and the commit you tried;
- what an attacker could do with it.

The report stays private until a fix is on `main`, and you are credited in the advisory unless you'd rather not be.

This covers Thermaestro's own code: the core, its plugins and `thermaestro-gateway`. A vulnerability in a dependency, in esphome-nibe or in a heat pump's own firmware belongs with that project or maker. Tell us too if it affects Thermaestro.
