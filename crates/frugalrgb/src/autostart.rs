//! Starting frugalRGB when the user logs in.
//!
//! Linux gets an XDG autostart entry. Windows gets a shortcut in the
//! Startup folder; when the program runs elevated the shortcut starts a
//! scheduled task instead, which brings frugalRGB up elevated at every
//! login without a UAC prompt (memory RGB needs that).

use std::io;
use std::path::PathBuf;

/// The file whose presence means "start at login", where that is supported.
fn entry() -> Option<PathBuf> {
    #[cfg(target_os = "linux")]
    {
        let config_home = std::env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".config")))?;
        Some(config_home.join("autostart").join("frugalrgb.desktop"))
    }
    #[cfg(windows)]
    {
        let appdata = PathBuf::from(std::env::var_os("APPDATA")?);
        Some(appdata.join(r"Microsoft\Windows\Start Menu\Programs\Startup\frugalRGB.lnk"))
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    {
        None
    }
}

pub fn supported() -> bool {
    entry().is_some()
}

pub fn enabled() -> bool {
    entry().is_some_and(|entry| entry.exists())
}

pub fn set(enabled: bool) -> io::Result<()> {
    let Some(entry) = entry() else {
        return Ok(());
    };
    if !enabled {
        return match std::fs::remove_file(&entry) {
            Err(e) if e.kind() != io::ErrorKind::NotFound => Err(e),
            _ => Ok(()),
        };
    }
    if let Some(dir) = entry.parent() {
        std::fs::create_dir_all(dir)?;
    }
    create(&entry)
}

#[cfg(target_os = "linux")]
fn create(entry: &std::path::Path) -> io::Result<()> {
    // A packaged binary is on the PATH; anything else is started by path
    let exe = std::env::current_exe()?;
    let command = if exe.starts_with("/usr/bin") { "frugalrgb".to_string() } else { exe.display().to_string() };
    std::fs::write(
        entry,
        format!(
            "[Desktop Entry]\nType=Application\nName=frugalRGB\nExec={command}\nIcon=frugalrgb\n\
             X-GNOME-Autostart-enabled=true\n"
        ),
    )
}

#[cfg(windows)]
const TASK: &str = "frugalRGB";

#[cfg(windows)]
fn run_hidden(program: &str, args: &[&str]) -> io::Result<()> {
    use std::os::windows::process::CommandExt;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    let output = std::process::Command::new(program).args(args).creation_flags(CREATE_NO_WINDOW).output()?;
    if output.status.success() {
        Ok(())
    } else {
        Err(io::Error::other(String::from_utf8_lossy(&output.stderr).trim().to_string()))
    }
}

#[cfg(windows)]
fn create(entry: &std::path::Path) -> io::Result<()> {
    let exe = std::env::current_exe()?;
    let exe = exe.display().to_string();

    // The task has a dummy schedule: it only exists to be started by name.
    // Creating it needs elevation, so an unelevated run links the program.
    let task_ready = frugalrgb_core::diagnostics::is_elevated()
        && run_hidden(
            "schtasks",
            &["/create", "/tn", TASK, "/tr", &format!("\"{exe}\""), "/sc", "ONCE", "/st", "00:00", "/rl", "HIGHEST", "/f"],
        )
        .map_err(|e| log::warn!("Could not create the scheduled task: {e}"))
        .is_ok();
    let (target, arguments) =
        if task_ready { ("schtasks.exe".to_string(), format!("/run /tn {TASK}")) } else { (exe, String::new()) };

    // Single-quoted PowerShell strings: only the quote itself needs escaping
    let quoted = |text: &str| format!("'{}'", text.replace('\'', "''"));
    let script = format!(
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut({}); $s.TargetPath = {}; \
         $s.Arguments = {}; $s.WindowStyle = 7; $s.Save()",
        quoted(&entry.display().to_string()),
        quoted(&target),
        quoted(&arguments)
    );
    run_hidden("powershell", &["-NoProfile", "-Command", &script])
}

#[cfg(not(any(target_os = "linux", windows)))]
fn create(_entry: &std::path::Path) -> io::Result<()> {
    Ok(())
}
