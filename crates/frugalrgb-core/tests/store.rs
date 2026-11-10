//! Preset and config files written by the Python app must load unchanged.

use frugalrgb_core::store::{self, Calibration, Config, Presets};
use frugalrgb_core::{Controller, Correction, DeviceInfo, Mode, Result, Zone, ZoneWrite};

struct Named {
    name: &'static str,
    zones: Vec<Zone>,
}

impl Controller for Named {
    fn name(&self) -> &str {
        self.name
    }
    fn zones(&self) -> &[Zone] {
        &self.zones
    }
    fn supported_modes(&self) -> &'static [Mode] {
        &Mode::ALL
    }
    fn hardware_animates(&self, _mode: Mode) -> bool {
        true
    }
    fn set_correction(&mut self, _correction: Correction) {}
    fn set_mode(&mut self, _mode: Mode, _speed: u8) -> Result<()> {
        Ok(())
    }
    fn write_zones(&mut self, _zones: &[ZoneWrite]) -> Result<()> {
        Ok(())
    }
}

fn devices() -> Vec<DeviceInfo> {
    let controllers: Vec<Box<dyn Controller>> = vec![
        Box::new(Named {
            name: "Board",
            zones: vec![Zone::all(0, "All Zones"), Zone::new(1, "Logo"), Zone::new(2, "Accent"), Zone::new(3, "D_LED4")],
        }),
        Box::new(Named { name: "Stick", zones: vec![Zone::new(0, "DRAM")] }),
    ];
    DeviceInfo::all(&controllers)
}

const PRESETS: &str = r#"{
  "Zebra": {
    "color": [255, 128, 0],
    "effect": "color_cycle",
    "speed": 2.5,
    "devices": {
      "Board|All Zones": {"enabled": true, "color": [255, 128, 0]},
      "Board|Logo": {"enabled": true, "color": [0, 0, 255]},
      "Board|Accent": {"enabled": false, "color": [0, 0, 0]},
      "Stick": {"enabled": false, "color": [1, 2, 3]}
    }
  },
  "Alpha": {"color": [9, 8, 7]}
}"#;

fn presets() -> Presets {
    serde_json::from_str(PRESETS).unwrap()
}

#[test]
fn presets_keep_the_order_they_were_saved_in() {
    assert_eq!(presets().keys().collect::<Vec<_>>(), ["Zebra", "Alpha"]);
}

#[test]
fn preset_cards_follow_the_saved_zone_states() {
    let presets = presets();
    let zebra = &presets["Zebra"];
    assert_eq!(zebra.mode(), Mode::ColorCycle);
    assert_eq!(zebra.speed(), 2.5);

    let cards = zebra.cards(&devices());
    let summary: Vec<_> = cards.iter().map(|c| (c.device, c.zone, c.enabled, c.color)).collect();
    assert_eq!(
        summary,
        [
            (0, 0, true, [255, 128, 0]),
            (0, 1, true, [0, 0, 255]),
            (0, 2, false, [0, 0, 0]),
            // A zone the preset has never seen is lit with its global colour
            (0, 3, true, [255, 128, 0]),
            // Single-zone devices are keyed by the device name alone
            (1, 0, false, [1, 2, 3]),
        ]
    );
}

#[test]
fn preset_from_before_per_zone_colours_lights_everything() {
    let presets = presets();
    let alpha = &presets["Alpha"];
    assert_eq!(alpha.mode(), Mode::Static);
    assert_eq!(alpha.speed(), 1.0);
    assert!(alpha.cards(&devices()).iter().all(|c| c.enabled && c.color == [9, 8, 7]));
}

#[test]
fn config_reads_per_device_and_legacy_calibration() {
    let config: Config = serde_json::from_str(
        r#"{"calibration": {"Board": [1.0, 0.262, 1.0]}, "startup_preset": "Zebra",
            "minimize_to_tray": true, "window_geometry": "947x1007+3284+1463"}"#,
    )
    .unwrap();
    assert_eq!(config.calibration.for_device("Board"), Correction([1.0, 0.262, 1.0]));
    assert_eq!(config.calibration.for_device("Other"), Correction::default());
    assert_eq!(config.startup_preset(), Some("Zebra"));
    assert!(config.minimize_to_tray);

    let legacy: Config = serde_json::from_str(r#"{"calibration": [0.5, 0.6, 0.7]}"#).unwrap();
    assert_eq!(legacy.calibration, Calibration::Global([0.5, 0.6, 0.7]));
    assert_eq!(legacy.calibration.for_device("Anything"), Correction([0.5, 0.6, 0.7]));
    assert_eq!(legacy.startup_preset(), None);
}

#[test]
fn saving_keeps_settings_this_version_does_not_know() {
    let dir = std::env::temp_dir().join(format!("frugalrgb-store-test-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let path = dir.join("config.json");
    std::fs::write(&path, r#"{"startup_preset": "(none)", "from_the_future": "700x600+1+2"}"#).unwrap();

    let mut config = store::load_config(&path).unwrap();
    assert_eq!(config.startup_preset(), None);
    config.off_on_close = true;
    store::save_config(&path, &config).unwrap();

    let again = store::load_config(&path).unwrap();
    assert!(again.off_on_close);
    assert_eq!(again.other["from_the_future"], "700x600+1+2");
    assert!(!dir.join("config.json.tmp").exists());

    // A missing file is a default, a broken one is an error
    assert_eq!(store::load_presets(&dir.join("absent.json")).unwrap(), Presets::new());
    std::fs::write(&path, "{ not json").unwrap();
    assert!(store::load_config(&path).is_err());
    std::fs::remove_dir_all(&dir).unwrap();
}

#[test]
fn window_size_comes_from_the_geometry_the_python_app_wrote() {
    let config: Config = serde_json::from_str(r#"{"window_geometry": "947x1007+3284+1463"}"#).unwrap();
    assert_eq!(config.window_size(), Some((947, 1007)));

    let mut config = Config::default();
    assert_eq!(config.window_size(), None);
    config.set_window_size(860, 680);
    assert_eq!(config.window_size(), Some((860, 680)));

    // Nonsense from a hand-edited file is ignored
    let config: Config = serde_json::from_str(r#"{"window_geometry": "1x1+0+0"}"#).unwrap();
    assert_eq!(config.window_size(), None);
}
