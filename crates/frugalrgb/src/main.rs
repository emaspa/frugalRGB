//! frugalRGB: a window and tray icon for your RGB hardware, and the same
//! thing as plain commands for scripts and login.

use std::process::ExitCode;

mod cli;
#[cfg(feature = "gui")]
mod gui;

const USAGE: &str = "\
frugalrgb - no bloat, just LEDs

Usage: frugalrgb [-v] [command]

Without a command the window opens.

Commands:
  list             Show detected devices and their zones
  presets          Show saved presets
  apply [PRESET]   Apply a preset (default: the startup preset) and exit
  off              Switch every LED off

Options:
  -v, --verbose    Log what is being detected and written
  -h, --help       Show this help
  -V, --version    Show the version";

struct StderrLog;

impl log::Log for StderrLog {
    fn enabled(&self, metadata: &log::Metadata) -> bool {
        // Only our own crates: the UI toolkit is chatty at debug level
        metadata.level() <= log::max_level() && metadata.target().starts_with("frugalrgb")
    }

    fn log(&self, record: &log::Record) {
        if self.enabled(record.metadata()) {
            eprintln!("{} {}", record.level(), record.args());
        }
    }

    fn flush(&self) {}
}

#[cfg(feature = "gui")]
fn window() -> Result<(), String> {
    gui::run()
}

#[cfg(not(feature = "gui"))]
fn window() -> Result<(), String> {
    Err(format!("this build has no window\n\n{USAGE}"))
}

fn main() -> ExitCode {
    let mut verbose = false;
    let mut words: Vec<String> = Vec::new();
    for arg in std::env::args().skip(1) {
        match arg.as_str() {
            "-v" | "--verbose" => verbose = true,
            "-h" | "--help" => {
                println!("{USAGE}");
                return ExitCode::SUCCESS;
            }
            "-V" | "--version" => {
                println!("frugalrgb {}", env!("CARGO_PKG_VERSION"));
                return ExitCode::SUCCESS;
            }
            _ => words.push(arg),
        }
    }

    let words: Vec<&str> = words.iter().map(String::as_str).collect();

    // Commands print their result and nothing else; the window keeps a log
    let quiet = if words.is_empty() { log::LevelFilter::Info } else { log::LevelFilter::Warn };
    let _ = log::set_boxed_logger(Box::new(StderrLog));
    log::set_max_level(if verbose { log::LevelFilter::Debug } else { quiet });
    let result = match words.as_slice() {
        [] => window(),
        ["list"] => cli::list(),
        ["presets"] => cli::presets(),
        // --apply-quit is what login entries made by earlier versions run
        ["apply"] | ["--apply-quit"] => cli::apply(None),
        ["apply", name] => cli::apply(Some(name)),
        ["off"] => cli::off(),
        _ => {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("frugalrgb: {message}");
            ExitCode::FAILURE
        }
    }
}
