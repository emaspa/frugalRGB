fn main() {
    #[cfg(feature = "gui")]
    {
        let config = slint_build::CompilerConfiguration::new().with_style("fluent-dark".into());
        slint_build::compile_with_config("ui/app.slint", config).expect("ui/app.slint compiles");
    }
}
