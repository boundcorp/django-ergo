//! Proposed upstream seam; used unchanged by the std-only regression suite.
//! This validates a launcher-owned directory, not a sandbox or a race-proof API.
use std::ffi::OsStr;
use std::io;
use std::path::{Component, Path, PathBuf};

pub fn resolve_runtime_home(home: &Path, configured: Option<&OsStr>) -> io::Result<PathBuf> {
    let Some(configured) = configured else {
        return Ok(home.to_path_buf());
    };
    let root = Path::new(configured);
    let invalid = || {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "CODEX_RUNTIME_HOME must be an existing absolute directory without symlinks, separate from CODEX_HOME",
        )
    };
    if configured.is_empty() || !root.is_absolute() {
        return Err(invalid());
    }
    let mut prefix = PathBuf::new();
    for component in root.components() {
        if matches!(component, Component::ParentDir | Component::CurDir) {
            return Err(invalid());
        }
        prefix.push(component.as_os_str());
        let metadata = std::fs::symlink_metadata(&prefix)?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err(invalid());
        }
    }
    let root = root.canonicalize()?;
    let home = home.canonicalize()?;
    if root.starts_with(&home) || home.starts_with(&root) {
        return Err(invalid());
    }
    Ok(root)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    static NEXT: AtomicUsize = AtomicUsize::new(0);
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let base = std::env::var_os("CODEX_FIXTURE_ROOT")
                .unwrap_or_else(|| std::env::temp_dir().into_os_string());
            let root = PathBuf::from(base).join(format!(
                "test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&root).unwrap();
            std::fs::create_dir(root.join("home")).unwrap();
            std::fs::create_dir(root.join("state")).unwrap();
            Self(root)
        }
        fn home(&self) -> PathBuf {
            self.0.join("home")
        }
        fn state(&self) -> PathBuf {
            self.0.join("state")
        }
        fn resolve(&self, path: &Path) -> io::Result<PathBuf> {
            resolve_runtime_home(&self.home(), Some(path.as_os_str()))
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            std::fs::remove_dir_all(&self.0).unwrap();
        }
    }
    #[test]
    fn default_does_not_stat_or_create_home() {
        let p = Path::new("/nonexistent-synthetic-codex-home");
        assert_eq!(resolve_runtime_home(p, None).unwrap(), p);
    }
    #[test]
    fn explicit_root_preserves_synthetic_auth_and_config() {
        let f = Fixture::new();
        for name in ["auth.json", "config.toml"] {
            std::fs::write(f.home().join(name), b"synthetic-sentinel").unwrap();
        }
        assert_eq!(
            f.resolve(&f.state()).unwrap(),
            f.state().canonicalize().unwrap()
        );
        assert_eq!(std::fs::read_dir(f.state()).unwrap().count(), 0);
        for name in ["auth.json", "config.toml"] {
            assert_eq!(
                std::fs::read(f.home().join(name)).unwrap(),
                b"synthetic-sentinel"
            );
        }
    }
    #[test]
    fn rejects_empty_and_relative() {
        let f = Fixture::new();
        for path in ["", "state", "../state"] {
            assert!(f.resolve(Path::new(path)).is_err());
        }
    }
    #[test]
    fn rejects_missing_without_creating_it() {
        let f = Fixture::new();
        let path = f.0.join("missing");
        assert!(f.resolve(&path).is_err());
        assert!(!path.exists());
    }
    #[test]
    fn rejects_file() {
        let f = Fixture::new();
        let path = f.0.join("file");
        std::fs::write(&path, "synthetic").unwrap();
        assert!(f.resolve(&path).is_err());
    }
    #[test]
    fn rejects_parent_traversal() {
        let f = Fixture::new();
        assert!(f.resolve(&f.state().join("../state")).is_err());
    }
    #[test]
    fn rejects_auth_home_and_overlapping_roots() {
        let f = Fixture::new();
        let nested = f.home().join("runtime");
        std::fs::create_dir(&nested).unwrap();
        for path in [f.home(), nested, f.0.clone()] {
            assert!(f.resolve(&path).is_err());
        }
    }
    #[cfg(unix)]
    #[test]
    fn rejects_root_symlink() {
        let f = Fixture::new();
        let link = f.0.join("link");
        std::os::unix::fs::symlink(f.state(), &link).unwrap();
        assert!(f.resolve(&link).is_err());
    }
    #[cfg(unix)]
    #[test]
    fn rejects_ancestor_symlink() {
        let f = Fixture::new();
        let link = f.0.join("link");
        std::os::unix::fs::symlink(&f.0, &link).unwrap();
        assert!(f.resolve(&link.join("state")).is_err());
    }
    #[test]
    fn separate_jobs_have_distinct_physical_roots() {
        let first = Fixture::new();
        let second = Fixture::new();
        assert_ne!(
            first.resolve(&first.state()).unwrap(),
            second.resolve(&second.state()).unwrap()
        );
    }
}
