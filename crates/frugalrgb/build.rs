fn main() {
    #[cfg(feature = "gui")]
    {
        let config = slint_build::CompilerConfiguration::new().with_style("fluent-dark".into());
        slint_build::compile_with_config("ui/app.slint", config).expect("compiling ui/app.slint");
    }

    // The icon Explorer and the taskbar show for the .exe
    #[cfg(windows)]
    if std::env::var("CARGO_CFG_TARGET_OS").as_deref() == Ok("windows") {
        println!("cargo:rerun-if-changed=../../packaging/frugalrgb.ico");
        winresource::WindowsResource::new()
            .set_icon("../../packaging/frugalrgb.ico")
            .compile()
            .expect("embedding the icon");
    }
}
