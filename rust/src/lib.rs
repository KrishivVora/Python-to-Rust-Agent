//! Rust translation of `reference/version.py` (python-semver), std-only.
//!
//! Implements parsing, formatting, semver precedence comparison and the
//! three numeric bump operations for a `Version` struct that mirrors the
//! public surface of python-semver's `Version` class.

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

/// Read a `0|[1-9]\d*` numeric core component starting at `*idx`, advancing
/// `*idx` past it. Returns the matched digit string, or an error if there
/// are no digits at that position or the digits have a disallowed leading
/// zero.
fn take_numeric_component(s: &str, idx: &mut usize) -> Result<String, String> {
    let bytes = s.as_bytes();
    let start = *idx;
    while *idx < bytes.len() && bytes[*idx].is_ascii_digit() {
        *idx += 1;
    }
    if *idx == start {
        return Err("expected a numeric version component".to_string());
    }
    let comp = &s[start..*idx];
    if comp.len() > 1 && comp.as_bytes()[0] == b'0' {
        return Err("numeric component has a leading zero".to_string());
    }
    Ok(comp.to_string())
}

/// A single dot-separated prerelease identifier is valid if it consists
/// only of ASCII alphanumerics and hyphens, and, if it is purely numeric,
/// has no leading zero (unless it is exactly "0").
fn is_valid_prerelease_identifier(id: &str) -> bool {
    if id.is_empty() {
        return false;
    }
    if !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-') {
        return false;
    }
    let all_digits = id.bytes().all(|b| b.is_ascii_digit());
    if all_digits {
        id == "0" || id.as_bytes()[0] != b'0'
    } else {
        true
    }
}

/// A single dot-separated build identifier is valid if it is a non-empty
/// run of ASCII alphanumerics and hyphens.
fn is_valid_build_identifier(id: &str) -> bool {
    !id.is_empty() && id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

/// Validate a dot-separated sequence of identifiers, all of which must
/// satisfy `validator`. An empty overall string is always invalid (the
/// grammar requires at least one identifier).
fn validate_dotted(s: &str, validator: fn(&str) -> bool) -> Result<(), String> {
    if s.is_empty() {
        return Err("empty identifier sequence".to_string());
    }
    for part in s.split('.') {
        if !validator(part) {
            return Err(format!("{part:?} is not a valid identifier"));
        }
    }
    Ok(())
}

/// Parse a version string. Return `Err` with a short reason for anything
/// that is not a valid semver 2.0.0 string.
///
/// Reject, among others: leading zeroes (`01.0.0`), missing components
/// (`1.0`), negative numbers, empty identifiers (`1.0.0-`), and whitespace.
pub fn parse(s: &str) -> Result<Version, String> {
    let bytes = s.as_bytes();
    let mut idx = 0usize;

    let major_str = take_numeric_component(s, &mut idx)?;
    if idx >= bytes.len() || bytes[idx] != b'.' {
        return Err(format!("{s} is not valid SemVer string"));
    }
    idx += 1;

    let minor_str = take_numeric_component(s, &mut idx)?;
    if idx >= bytes.len() || bytes[idx] != b'.' {
        return Err(format!("{s} is not valid SemVer string"));
    }
    idx += 1;

    let patch_str = take_numeric_component(s, &mut idx)?;

    let mut prerelease: Option<String> = None;
    let mut build: Option<String> = None;

    if idx < bytes.len() && bytes[idx] == b'-' {
        idx += 1;
        let start = idx;
        while idx < bytes.len() && bytes[idx] != b'+' {
            idx += 1;
        }
        let pre = &s[start..idx];
        validate_dotted(pre, is_valid_prerelease_identifier)
            .map_err(|_| format!("{s} is not valid SemVer string"))?;
        prerelease = Some(pre.to_string());
    }

    if idx < bytes.len() && bytes[idx] == b'+' {
        idx += 1;
        let start = idx;
        idx = bytes.len();
        let b = &s[start..idx];
        validate_dotted(b, is_valid_build_identifier)
            .map_err(|_| format!("{s} is not valid SemVer string"))?;
        build = Some(b.to_string());
    }

    if idx != bytes.len() {
        return Err(format!("{s} is not valid SemVer string"));
    }

    let major = major_str
        .parse::<u64>()
        .map_err(|_| format!("{s}: major version out of range"))?;
    let minor = minor_str
        .parse::<u64>()
        .map_err(|_| format!("{s}: minor version out of range"))?;
    let patch = patch_str
        .parse::<u64>()
        .map_err(|_| format!("{s}: patch version out of range"))?;

    Ok(Version {
        major,
        minor,
        patch,
        prerelease,
        build,
    })
}

/// Render a `Version` back to its canonical string form.
/// `parse(&to_string(&v))` must round-trip for every valid `v`.
pub fn to_string(v: &Version) -> String {
    let mut s = format!("{}.{}.{}", v.major, v.minor, v.patch);
    if let Some(pre) = &v.prerelease {
        if !pre.is_empty() {
            s.push('-');
            s.push_str(pre);
        }
    }
    if let Some(build) = &v.build {
        if !build.is_empty() {
            s.push('+');
            s.push_str(build);
        }
    }
    s
}

/// Classification of a single dot-separated prerelease identifier for
/// natural-order comparison purposes.
enum Tag<'a> {
    Num(&'a str),
    Str(&'a str),
}

fn classify(id: &str) -> Tag<'_> {
    if !id.is_empty() && id.bytes().all(|b| b.is_ascii_digit()) {
        Tag::Num(id)
    } else {
        Tag::Str(id)
    }
}

/// Compare two digit-only strings (no leading zeros, may be arbitrarily
/// long) as arbitrary-precision non-negative integers.
fn cmp_numeric_str(a: &str, b: &str) -> Ordering {
    match a.len().cmp(&b.len()) {
        Ordering::Equal => a.cmp(b),
        other => other,
    }
}

fn cmp_tag(a: &Tag, b: &Tag) -> Ordering {
    match (a, b) {
        (Tag::Num(x), Tag::Num(y)) => cmp_numeric_str(x, y),
        (Tag::Num(_), Tag::Str(_)) => Ordering::Less,
        (Tag::Str(_), Tag::Num(_)) => Ordering::Greater,
        (Tag::Str(x), Tag::Str(y)) => x.cmp(y),
    }
}

/// Mirror of `Version._nat_cmp`: compare two (possibly absent) prerelease
/// strings identifier-by-identifier, dot-separated, treating an absent
/// prerelease the same as an empty one.
fn nat_cmp(a: Option<&str>, b: Option<&str>) -> Ordering {
    let a_str = a.unwrap_or("");
    let b_str = b.unwrap_or("");
    let a_parts: Vec<&str> = a_str.split('.').collect();
    let b_parts: Vec<&str> = b_str.split('.').collect();

    for (pa, pb) in a_parts.iter().zip(b_parts.iter()) {
        let c = cmp_tag(&classify(pa), &classify(pb));
        if c != Ordering::Equal {
            return c;
        }
    }
    a_parts.len().cmp(&b_parts.len())
}

fn is_falsy(s: &Option<String>) -> bool {
    match s {
        None => true,
        Some(x) => x.is_empty(),
    }
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
pub fn compare(a: &Version, b: &Version) -> Ordering {
    let core = (a.major, a.minor, a.patch).cmp(&(b.major, b.minor, b.patch));
    if core != Ordering::Equal {
        return core;
    }

    let rccmp = nat_cmp(a.prerelease.as_deref(), b.prerelease.as_deref());
    if rccmp == Ordering::Equal {
        return Ordering::Equal;
    }
    if is_falsy(&a.prerelease) {
        return Ordering::Greater;
    }
    if is_falsy(&b.prerelease) {
        return Ordering::Less;
    }
    rccmp
}

/// Increment major; reset minor and patch; drop prerelease and build.
pub fn bump_major(v: &Version) -> Version {
    Version {
        major: v.major.saturating_add(1),
        minor: 0,
        patch: 0,
        prerelease: None,
        build: None,
    }
}

/// Increment minor; reset patch; drop prerelease and build.
pub fn bump_minor(v: &Version) -> Version {
    Version {
        major: v.major,
        minor: v.minor.saturating_add(1),
        patch: 0,
        prerelease: None,
        build: None,
    }
}

/// Increment patch; drop prerelease and build.
///
/// Do not guess the prerelease interaction — read `reference/version.py`
/// and check against the oracle. `evaluate.py` compares you to the real
/// `semver` package on every bump of every valid version it generates.
pub fn bump_patch(v: &Version) -> Version {
    Version {
        major: v.major,
        minor: v.minor,
        patch: v.patch.saturating_add(1),
        prerelease: None,
        build: None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_full_version() -> Result<(), String> {
        let v = parse("1.2.3-alpha.1.2+build.11.e0f985a")?;
        assert_eq!(
            v,
            Version {
                major: 1,
                minor: 2,
                patch: 3,
                prerelease: Some("alpha.1.2".to_string()),
                build: Some("build.11.e0f985a".to_string()),
            }
        );
        Ok(())
    }

    #[test]
    fn parse_prerelease_with_hyphen() -> Result<(), String> {
        let v = parse("1.2.3-alpha-1+build.11.e0f985a")?;
        assert_eq!(v.prerelease, Some("alpha-1".to_string()));
        assert_eq!(v.build, Some("build.11.e0f985a".to_string()));
        Ok(())
    }

    #[test]
    fn parse_leading_digit_prerelease() -> Result<(), String> {
        let v = parse("0.1.0-0f")?;
        assert_eq!(
            v,
            Version {
                major: 0,
                minor: 1,
                patch: 0,
                prerelease: Some("0f".to_string()),
                build: None,
            }
        );
        Ok(())
    }

    #[test]
    fn parse_zero_prerelease_multi() -> Result<(), String> {
        let v = parse("1.2.3-rc.0.0+build.0")?;
        assert_eq!(v.prerelease, Some("rc.0.0".to_string()));
        assert_eq!(v.build, Some("build.0".to_string()));
        Ok(())
    }

    #[test]
    fn parse_no_prerelease_no_build() -> Result<(), String> {
        let v = parse("1.2.3")?;
        assert_eq!(
            v,
            Version {
                major: 1,
                minor: 2,
                patch: 3,
                prerelease: None,
                build: None,
            }
        );
        Ok(())
    }

    #[test]
    fn reject_zero_prefixed_versions() {
        for bad in ["01.2.3", "1.02.3", "1.2.03"] {
            assert!(parse(bad).is_err(), "{bad} should be rejected");
        }
    }

    #[test]
    fn reject_missing_parts() {
        for bad in ["foo", "1.0", "1.x", "1", "1.2.3-", "1.2.3+", "1.2.3.4"] {
            assert!(parse(bad).is_err(), "{bad} should be rejected");
        }
    }

    #[test]
    fn round_trip_formatting() -> Result<(), String> {
        let s = "1.2.3-alpha.1.2+build.11.e0f985a";
        let v = parse(s)?;
        assert_eq!(to_string(&v), s);
        Ok(())
    }

    #[test]
    fn round_trip_no_prerelease() -> Result<(), String> {
        let s = "3.4.5";
        let v = parse(s)?;
        assert_eq!(to_string(&v), s);
        Ok(())
    }

    #[test]
    fn compare_spec_chain_is_ascending() -> Result<(), String> {
        let chain = [
            "1.0.0-alpha",
            "1.0.0-alpha.1",
            "1.0.0-alpha.beta",
            "1.0.0-beta",
            "1.0.0-beta.2",
            "1.0.0-beta.11",
            "1.0.0-rc.1",
            "1.0.0",
        ];
        for pair in chain.windows(2) {
            let low = parse(pair[0])?;
            let high = parse(pair[1])?;
            assert_eq!(
                compare(&low, &high),
                Ordering::Less,
                "{} < {}",
                pair[0],
                pair[1]
            );
            assert_eq!(
                compare(&high, &low),
                Ordering::Greater,
                "{} > {}",
                pair[1],
                pair[0]
            );
        }
        Ok(())
    }

    #[test]
    fn compare_ignores_build_metadata() -> Result<(), String> {
        let cases = [
            ("1.0.0+build.1", "1.0.0", Ordering::Equal),
            ("1.0.0-alpha.1+build.1", "1.0.0-alpha.1", Ordering::Equal),
            ("1.0.0+build.1", "1.0.0-alpha.1", Ordering::Greater),
            ("1.0.0+build.1", "1.0.0-alpha.1+build.1", Ordering::Greater),
        ];
        for (l, r, expected) in cases {
            let lv = parse(l)?;
            let rv = parse(r)?;
            assert_eq!(compare(&lv, &rv), expected, "{l} vs {r}");
        }
        Ok(())
    }

    #[test]
    fn compare_equal_versions() -> Result<(), String> {
        let cases = [
            ("2.0.0", "2.0.0"),
            ("1.1.9-rc.1", "1.1.9-rc.1"),
            ("1.1.9+build.1", "1.1.9+build.1"),
            ("1.1.9-rc.1+build.1", "1.1.9-rc.1+build.1"),
        ];
        for (l, r) in cases {
            let lv = parse(l)?;
            let rv = parse(r)?;
            assert_eq!(compare(&lv, &rv), Ordering::Equal, "{l} vs {r}");
        }
        Ok(())
    }

    #[test]
    fn compare_rc_numeric_identifiers() -> Result<(), String> {
        let a = parse("1.0.0-beta.2")?;
        let b = parse("1.0.0-beta.11")?;
        assert_eq!(compare(&a, &b), Ordering::Less);
        Ok(())
    }

    #[test]
    fn compare_rc1_vs_rc0() -> Result<(), String> {
        let a = parse("1.0.0-rc1")?;
        let b = parse("1.0.0-rc0")?;
        assert_eq!(compare(&a, &b), Ordering::Greater);
        Ok(())
    }

    #[test]
    fn compare_numeric_vs_alphanumeric_prerelease() -> Result<(), String> {
        // "1unms" splits into identifier "1unms" (alnum, not pure digits) so
        // it is a Str tag; "1" from the build side is irrelevant since
        // build is ignored, so this compares prerelease "1unms" (present)
        // against no prerelease at all -> lower precedence.
        let a = parse("1.9.1-1unms")?;
        let b = parse("1.9.1+1")?;
        assert_eq!(compare(&a, &b), Ordering::Less);
        Ok(())
    }

    #[test]
    fn bump_major_resets_lower_parts() -> Result<(), String> {
        let v = parse("3.4.5")?;
        assert_eq!(
            bump_major(&v),
            Version {
                major: 4,
                minor: 0,
                patch: 0,
                prerelease: None,
                build: None,
            }
        );
        Ok(())
    }

    #[test]
    fn bump_minor_resets_patch() -> Result<(), String> {
        let v = parse("3.4.5")?;
        assert_eq!(
            bump_minor(&v),
            Version {
                major: 3,
                minor: 5,
                patch: 0,
                prerelease: None,
                build: None,
            }
        );
        Ok(())
    }

    #[test]
    fn bump_patch_increments_only_patch() -> Result<(), String> {
        let v = parse("3.4.5")?;
        assert_eq!(
            bump_patch(&v),
            Version {
                major: 3,
                minor: 4,
                patch: 6,
                prerelease: None,
                build: None,
            }
        );
        Ok(())
    }

    #[test]
    fn bump_ignores_prerelease_and_build() -> Result<(), String> {
        let v = parse("3.4.5-rc1+build4")?;
        assert_eq!(to_string(&bump_patch(&v)), "3.4.6");
        Ok(())
    }

    #[test]
    fn bump_major_then_minor() -> Result<(), String> {
        let v = parse("3.4.5")?;
        let bumped = bump_minor(&bump_major(&v));
        let expected = parse("4.1.0")?;
        assert_eq!(bumped, expected);
        Ok(())
    }

    #[test]
    fn simple_bump_functions_from_string() -> Result<(), String> {
        assert_eq!(to_string(&bump_major(&parse("3.4.5")?)), "4.0.0");
        assert_eq!(to_string(&bump_minor(&parse("3.4.5")?)), "3.5.0");
        assert_eq!(to_string(&bump_patch(&parse("3.4.5")?)), "3.4.6");
        Ok(())
    }

    #[test]
    fn no_leading_zero_in_identifiers_rejected() {
        assert!(parse("1.2.3-01").is_err());
        assert!(parse("1.2.3+_bad_").is_err());
    }

    #[test]
    fn parse_rejects_empty_string() {
        assert!(parse("").is_err());
    }

    #[test]
    fn parse_large_numbers_fit_u64() -> Result<(), String> {
        let v = parse("18446744073709551615.0.0")?;
        assert_eq!(v.major, u64::MAX);
        Ok(())
    }

    #[test]
    fn parse_rejects_numbers_too_large_for_u64() {
        assert!(parse("18446744073709551616.0.0").is_err());
    }
}
