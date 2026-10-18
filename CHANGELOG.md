# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - unreleased
### Added
- First public release: per-call logging, monthly report and reconciliation, optional HTTP ingest.
### Security
- Ingest validates every field, rejects oversized or malformed bodies, drops slow clients and requires a token of at least 16 characters.
- Database files are created readable by their owner only; the hostname is never recorded.
