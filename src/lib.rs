use pyo3::prelude::*;

#[pymodule]
fn sylva(_py: Python, _m: &Bound<'_, PyModule>) -> PyResult<()> {
    Ok(())
}
