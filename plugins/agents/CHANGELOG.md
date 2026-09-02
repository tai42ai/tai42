# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Async-park ownership is now checked at the claim point, and a park in flight when a
  release-predecessor's build is upgraded still resumes. A park written by an earlier build
  carries no `resume_owner` on its wire marker; when the driver resumes it, the resuming
  super-step names the interactions it is delivering answers for, so the claim check adopts the
  ownerless in-flight park instead of refusing it and dropping the operator's answer. No action
  is required on upgrade — an in-flight park resumes on the new build. Rollback is safe by
  construction too: the `resume_owner` key is additive on the wire marker, so an older reader
  simply ignores it and reads the park exactly as before.
