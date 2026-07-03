use pyo3::prelude::*;

mod db;
mod extractor;
mod mcp;
mod walker;
mod writer;

#[pymodule]
fn sylva(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(db::init_db, m)?)?;
    m.add_function(wrap_pyfunction!(walker::walk_python_files, m)?)?;
    m.add_function(wrap_pyfunction!(extractor::extract_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(writer::write_symbols, m)?)?;
    m.add_function(wrap_pyfunction!(mcp::handle_request, m)?)?;
    Ok(())
}
