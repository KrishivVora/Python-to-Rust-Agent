//! THE FILE YOUR AGENT MUST WRITE.
//!
//! This is the Rust translation of `reference/version.py`. The signatures
//! below are fixed — `src/main.rs` calls them and `evaluate.py` speaks to
//! `main.rs`. You may add anything you like *in addition* to these.
//!
//! It compiles as given. It fails every behavioural test as given.
//!
//! ---------------------------------------------------------------------
//! Rules your translation must satisfy (see README):
//!   * no `unsafe`
//!   * no calling back into Python
//!   * no dependencies — std only (Cargo.toml has none; keep it that way)
//!   * `todo!()` / `unimplemented!()` count as "not translated"
//! ---------------------------------------------------------------------

use std::cmp::Ordering;

/// A parsed semantic version.
///
/// `prerelease` and `build` hold the text *after* the `-` and `+`
/// respectively, with the separator stripped, or `None` when absent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Version {
    pub major: u64,
    pub minor: u64,
    pub patch: u64,
    pub prerelease: Option<String>,
    pub build: Option<String>,
}

/// Parse a version string. Return `Err` with a short reason for anything
/// that is not a valid semver 2.0.0 string.
///
/// Reject, among others: leading zeroes (`01.0.0`), missing components
/// (`1.0`), negative numbers, empty identifiers (`1.0.0-`), and whitespace.
pub fn parse(_s: &str) -> Result<Version, String> {
    // TODO(agent): implement.
    todo!("parse")
}

/// Render a `Version` back to its canonical string form.
/// `parse(&to_string(&v))` must round-trip for every valid `v`.
pub fn to_string(_v: &Version) -> String {
    // TODO(agent): implement.
    todo!("to_string")
}

/// Semver precedence.
///
/// Careful — this is where naive translations break:
///   * build metadata is IGNORED entirely for precedence
///   * a version WITH a prerelease is LOWER than the same version without
///   * prerelease identifiers compare left to right, dot-separated
///   * all-numeric identifiers compare numerically
///   * all other identifiers compare lexically in ASCII order
///   * numeric identifiers always rank LOWER than non-numeric ones
///   * if all preceding identifiers are equal, more identifiers wins
///
/// The spec's own worked example, which you should be able to reproduce:
///   1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta
///     < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0
pub fn compare(_a: &Version, _b: &Version) -> Ordering {
    // TODO(agent): implement.
    todo!("compare")
}

/// Increment major; reset minor and patch; drop prerelease and build.
pub fn bump_major(_v: &Version) -> Version {
    // TODO(agent): implement.
    todo!("bump_major")
}

/// Increment minor; reset patch; drop prerelease and build.
pub fn bump_minor(_v: &Version) -> Version {
    // TODO(agent): implement.
    todo!("bump_minor")
}

/// Increment patch; drop prerelease and build.
///
/// Do not guess the prerelease interaction — read `reference/version.py`
/// and check against the oracle. `evaluate.py` compares you to the real
/// `semver` package on every bump of every valid version it generates.
pub fn bump_patch(_v: &Version) -> Version {
    // TODO(agent): implement.
    todo!("bump_patch")
}

#[cfg(test)]
mod tests {
    // TODO(agent): port the cases from reference/test_*.py here.
    // `cargo test` is part of your score.
    #[test]
    fn placeholder() {}
}
