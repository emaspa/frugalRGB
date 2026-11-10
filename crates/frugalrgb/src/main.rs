//! frugalRGB: a window and tray icon for your RGB hardware, and the same
//! thing as plain commands for scripts and login.

// A Windows GUI program: no console window opens behind the app
#![cfg_attr(all(windows, feature = "gui"), windows_subsystem = "windows")]

use std::process::ExitCode;

#[cfg(feature = "gui")]
mod autostart;
mod cli;
#[cfg(feature = "gui")]
mod gui;
mod logging;
#[cfg(feature = "gui")]
mod tray;

const USAGE: &str = "\
frugalrgb - no bloat, just LEDs

Usage: frugalrgb [-v] [command]

Without a command the window opens.

Commands:
  list             Show detected devices and their zones
  presets          Show saved presets
  apply [PRESET]   Apply a preset (default: the startup preset) and exit
  off              Switch every LED off
  test             Show red, green, blue and white on every device
  diagnostics      Print a report on the hardware for a bug report

Options:
  -v, --verbose    Log what is being detected and written
  -h, --help       Show this help
  -V, --version    Show the version";

#[cfg(feature = "gui")]
fn window() -> Result<(), String> {
    gui::run()
}

#[cfg(not(feature = "gui"))]
fn window() -> Result<(), String> {
    Err(format!("this build has no window\n\n{USAGE}"))
}

/// A Windows GUI program starts without a console. When a terminal started
/// it, borrow that terminal so the commands can print.
#[cfg(windows)]
fn attach_console() {
    #[link(name = "kernel32")]
    extern "system" {
        fn AttachConsole(process: u32) -> i32;
    }
    const ATTACH_PARENT_PROCESS: u32 = u32::MAX;
    // SAFETY: no pointers involved; failing just means there is no terminal
    unsafe { AttachConsole(ATTACH_PARENT_PROCESS) };
}

#[cfg(not(windows))]
fn attach_console() {}

fn main() -> ExitCode {
    attach_console();
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
    logging::init(if verbose { log::LevelFilter::Debug } else { quiet });
    let result = match words.as_slice() {
        [] => window(),
        ["list"] => cli::list(),
        ["presets"] => cli::presets(),
        // --apply-quit is what login entries made by earlier versions run
        ["apply"] | ["--apply-quit"] => cli::apply(None),
        ["apply", name] => cli::apply(Some(name)),
        ["off"] => cli::off(),
        ["test"] => cli::test(),
        ["diagnostics"] => cli::diagnostics(),
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
