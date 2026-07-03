use pyo3::prelude::*;

mod db;
mod extractor;
mod hash;
mod mcp;
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
    Ok(())
}
