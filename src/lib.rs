use pyo3::prelude::*;

/// Whether verbose per-item diagnostics are enabled. Off by default so normal
/// runs emit only bounded summaries (issue #39); set `SYLVA_LOG` to opt in to
/// per-reference / per-path detail.
pub(crate) fn verbose() -> bool {
    std::env::var("SYLVA_LOG").is_ok()
}

mod architecture;
mod coverage;
mod db;
mod edges;
mod entrypoints;
mod extractor;
mod foreign;
mod graph;
mod hash;
mod layers;
mod mcp;
mod spine;
mod testmap;
mod walker;
mod watcher;
mod writer;

#[pymodule]
fn sylva(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(db::init_db, m)?)?;
    m.add_function(wrap_pyfunction!(walker::walk_python_files, m)?)?;
    m.add_function(wrap_pyfunction!(extractor::extract_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(foreign::extract_foreign_exports, m)?)?;
    m.add_function(wrap_pyfunction!(writer::write_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(mcp::handle_request, m)?)?;
    m.add_function(wrap_pyfunction!(mcp::init_mcp, m)?)?;
    m.add_function(wrap_pyfunction!(hash::file_hash, m)?)?;
    m.add_function(wrap_pyfunction!(hash::file_needs_reindex, m)?)?;
    m.add_function(wrap_pyfunction!(hash::mark_indexed, m)?)?;
    m.add_function(wrap_pyfunction!(watcher::reindex_path, m)?)?;
    m.add_function(wrap_pyfunction!(watcher::handle_delete, m)?)?;
    m.add_function(wrap_pyfunction!(watcher::start_watcher, m)?)?;
    m.add_class::<watcher::WatcherHandle>()?;
    m.add_function(wrap_pyfunction!(coverage::parse_coverage, m)?)?;
    m.add_function(wrap_pyfunction!(coverage::apply_coverage, m)?)?;
    m.add_function(wrap_pyfunction!(coverage::get_module_coverage, m)?)?;
    m.add_function(wrap_pyfunction!(testmap::map_tests_to_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(edges::build_edges, m)?)?;
    m.add_function(wrap_pyfunction!(edges::build_dataflow, m)?)?;
    m.add_function(wrap_pyfunction!(edges::trace_calls, m)?)?;
    m.add_function(wrap_pyfunction!(edges::blast_radius, m)?)?;
    m.add_function(wrap_pyfunction!(architecture::get_architecture, m)?)?;
    m.add_function(wrap_pyfunction!(entrypoints::infer_entrypoints, m)?)?;
    m.add_function(wrap_pyfunction!(spine::main_spine, m)?)?;
    m.add_function(wrap_pyfunction!(layers::infer_layers, m)?)?;
    Ok(())
}
