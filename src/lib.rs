use pyo3::prelude::*;

mod coverage;
mod db;
mod edges;
mod extractor;
mod hash;
mod mcp;
mod testmap;
mod walker;
mod watcher;
mod writer;

#[pymodule]
fn sylva(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(db::init_db, m)?)?;
    m.add_function(wrap_pyfunction!(walker::walk_python_files, m)?)?;
    m.add_function(wrap_pyfunction!(extractor::extract_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(writer::write_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(mcp::handle_request, m)?)?;
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
    Ok(())
}
