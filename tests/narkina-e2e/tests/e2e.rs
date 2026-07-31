//! Headless E2E for the Telegram GTK wrapper, driven through narkina.
//!
//! The app is launched as `telegram.py --test` inside an isolated cage
//! session (headless backend, isolated HOME + XDG_RUNTIME_DIR, no
//! session DBus). Test IO goes through a per-session directory passed
//! via TELEGRAM_TEST_DIR:
//!   F11 — app reads a command from  $TELEGRAM_TEST_DIR/telegram-test-input.txt
//!   F12 — app dumps state to        $TELEGRAM_TEST_DIR/telegram-test-state.txt
//!
//! One #[test] runs the whole scenario: fixed file paths inside the
//! session mean steps are ordered, and a single cage launch keeps the
//! suite fast (WebKit + Telegram warmup dominates).

use narkina::{Modifier, Session};
use std::fs;
use std::path::PathBuf;
use std::time::{Duration, Instant};

const APP: &str = "../../telegram.py";
const WARMUP: Duration = Duration::from_millis(8000);
const CMD_DELAY: Duration = Duration::from_millis(800);

struct Harness {
    session: Session,
    test_dir: PathBuf,
}

impl Harness {
    fn launch() -> Self {
        let app = fs::canonicalize(APP).expect("telegram.py not found relative to test crate");
        let test_dir = std::env::temp_dir().join(format!("telegram-e2e-{}", std::process::id()));
        let _ = fs::remove_dir_all(&test_dir);
        fs::create_dir_all(&test_dir).unwrap();

        let session = Session::builder(&app)
            .arg("--test")
            .env("TELEGRAM_TEST_DIR", &test_dir)
            .stderr_to_file(test_dir.join("app-stderr.log"))
            .expect("stderr redirect failed")
            .spawn()
            .expect("failed to launch telegram.py under cage");
        session.sleep(WARMUP);
        Harness { session, test_dir }
    }

    fn input_file(&self) -> PathBuf {
        self.test_dir.join("telegram-test-input.txt")
    }

    fn state_file(&self) -> PathBuf {
        self.test_dir.join("telegram-test-state.txt")
    }

    /// Write a command + payload, press F11 to make the app execute it.
    fn send_cmd(&self, lines: &[&str]) {
        fs::write(self.input_file(), lines.join("\n")).unwrap();
        assert!(self.session.press_key("F11"), "wtype F11 failed");
        self.session.sleep(CMD_DELAY);
    }

    /// Read a key from the state file as last written (no F12 refresh) —
    /// used for command outputs like deeplink_url that F12 would overwrite.
    fn raw_state(&self, key: &str) -> String {
        let raw = fs::read_to_string(self.state_file()).unwrap_or_default();
        raw.lines()
            .filter_map(|l| l.split_once('='))
            .find(|(k, _)| *k == key)
            .map(|(_, v)| v.to_string())
            .unwrap_or_default()
    }

    /// Press F12, wait for the state dump, parse `key=value` lines.
    fn dump_state(&self) -> Vec<(String, String)> {
        let _ = fs::remove_file(self.state_file());
        assert!(self.session.press_key("F12"), "wtype F12 failed");
        let deadline = Instant::now() + Duration::from_secs(4);
        while !self.state_file().exists() {
            if Instant::now() >= deadline {
                let log = fs::read_to_string(self.test_dir.join("app-stderr.log"))
                    .unwrap_or_default();
                panic!("state file never appeared; app stderr:\n{log}");
            }
            std::thread::sleep(Duration::from_millis(150));
        }
        let raw = fs::read_to_string(self.state_file()).unwrap();
        raw.lines()
            .filter_map(|l| l.split_once('=').map(|(k, v)| (k.into(), v.into())))
            .collect()
    }

    fn state(&self, key: &str) -> String {
        self.dump_state()
            .into_iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v)
            .unwrap_or_default()
    }
}

impl Drop for Harness {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.test_dir);
    }
}

fn assert_alive(h: &mut Harness, what: &str) {
    h.session.assert_alive(what);
}

#[test]
fn full_scenario() {
    let mut h = Harness::launch();

    // ── P0: launch ──────────────────────────────────────────────
    assert_alive(&mut h, "app after warmup");
    let uri = h.state("uri");
    assert!(
        uri.contains("web.telegram.org"),
        "expected a web.telegram.org URI, got '{uri}'"
    );
    assert!(!h.state("title").is_empty(), "page should have a title");

    // Stock WebKitGTK UA (Safari-shaped) — telegram-tt's IS_SAFARI branch is
    // its tested path on this engine; a Chrome spoof enables broken paths.
    let ua = h.state("ua");
    assert!(
        ua.contains("Safari/") && !ua.contains("Chrome/"),
        "expected stock Safari-shaped UA, got '{ua}'"
    );

    // ── P1: deep-link conversion ────────────────────────────────
    h.send_cmd(&["deeplink", "tg://resolve?domain=telegram"]);
    let dl = h.raw_state("deeplink_url");
    assert!(
        dl.contains("tgaddr=") && dl.contains("telegram"),
        "tg:// deeplink not converted: '{dl}'"
    );

    h.send_cmd(&["deeplink", "https://t.me/telegram"]);
    let dl = h.raw_state("deeplink_url");
    assert!(
        dl.contains("tgaddr="),
        "t.me deeplink not converted: '{dl}'"
    );

    // Subdomain form (username.t.me) is a real Telegram feature.
    h.send_cmd(&["deeplink", "https://durov.t.me"]);
    let dl = h.raw_state("deeplink_url");
    assert!(
        dl.contains("tgaddr="),
        "subdomain t.me deeplink not converted: '{dl}'"
    );

    // Regression: unrecognized URI must not crash the app and must not convert.
    h.send_cmd(&["deeplink", "https://example.com/foo"]);
    let dl = h.raw_state("deeplink_url");
    assert!(dl.is_empty(), "non-Telegram URI wrongly converted: '{dl}'");
    assert_alive(&mut h, "app after unconvertible deeplink");

    // ── P2: downloads dir visibility ────────────────────────────
    let dl_dir = h.state("dl_dir");
    assert!(!dl_dir.is_empty(), "download dir should resolve");

    // ── P3: zoom ────────────────────────────────────────────────
    let zoom_before = h.state("zoom");
    h.session.key_combo(&[Modifier::Ctrl], "equal");
    h.session.sleep(Duration::from_millis(400));
    let zoom_after = h.state("zoom");
    assert_ne!(zoom_before, zoom_after, "Ctrl+= should change zoom");

    h.session.key_combo(&[Modifier::Ctrl], "0");
    h.session.sleep(Duration::from_millis(400));
    assert_eq!(h.state("zoom"), "1.0", "Ctrl+0 should reset zoom");

    // ── P4: JS eval + survival ──────────────────────────────────
    h.send_cmd(&["js", "document.title = document.title"]);
    assert_alive(&mut h, "app after JS eval");

    // ── P5: paste path survival (empty clipboard → failure branch) ──
    h.send_cmd(&["paste"]);
    assert_alive(&mut h, "app after paste with empty clipboard");

    // ── P6: window.open popup (related-view path) ───────────────
    h.send_cmd(&["js", "window.open('https://web.telegram.org/a/')"]);
    h.session.sleep(Duration::from_millis(1500));
    assert_alive(&mut h, "app after window.open popup");
}
