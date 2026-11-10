//! Log lines go to stderr and into a short memory that a diagnostics
//! report can quote.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Mutex;

/// How many lines the memory holds.
const KEEP: usize = 400;

static RECENT: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
/// The most verbose level that is printed, as a `log::LevelFilter`.
static PRINTED: AtomicUsize = AtomicUsize::new(log::LevelFilter::Warn as usize);

struct Logger;

impl log::Log for Logger {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        // Only our own crates: the UI toolkit is chatty at debug level
        metadata.target().starts_with("frugalrgb")
    }

    fn log(&self, record: &log::Record) {
        if !self.enabled(record.metadata()) {
            return;
        }
        let line = format!("{} {}", record.level(), record.args());
        if record.level() as usize <= PRINTED.load(Ordering::Relaxed) {
            eprintln!("{line}");
        }
        if let Ok(mut recent) = RECENT.lock() {
            if recent.len() == KEEP {
                recent.pop_front();
            }
            recent.push_back(line);
        }
    }

    fn flush(&self) {}
}

/// Start logging. Lines up to `printed` go to stderr; everything down to
/// debug level is remembered.
pub fn init(printed: log::LevelFilter) {
    PRINTED.store(printed as usize, Ordering::Relaxed);
    let _ = log::set_logger(&Logger);
    log::set_max_level(log::LevelFilter::Debug);
}

/// The last lines logged, oldest first.
pub fn recent() -> String {
    RECENT.lock().map(|recent| recent.iter().map(|line| format!("{line}\n")).collect()).unwrap_or_default()
}
