//! Small RTP parser exposed to Python through PyO3.
//!
//! This intentionally matches the supported subset in `voip.rtp.parse_rtp`:
//! fixed header plus a CSRC list, without padding or header extensions.

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;

const MINIMUM_HEADER_BYTES: usize = 12;

#[derive(Debug, PartialEq)]
struct ParsedFields<'a> {
    version: u8,
    padding: bool,
    extension: bool,
    csrc_count: u8,
    marker: bool,
    payload_type: u8,
    sequence_number: u16,
    timestamp: u32,
    ssrc: u32,
    header_length: usize,
    payload: &'a [u8],
}

fn parse_fields(data: &[u8]) -> Result<ParsedFields<'_>, String> {
    if data.len() < MINIMUM_HEADER_BYTES {
        return Err(format!(
            "RTP data too short: need at least {MINIMUM_HEADER_BYTES} bytes, got {}",
            data.len()
        ));
    }

    let byte0 = data[0];
    let byte1 = data[1];
    let version = (byte0 >> 6) & 0x03;
    let padding = byte0 & 0x20 != 0;
    let extension = byte0 & 0x10 != 0;
    let csrc_count = byte0 & 0x0f;
    let marker = byte1 & 0x80 != 0;
    let payload_type = byte1 & 0x7f;

    if version != 2 {
        return Err(format!("Invalid RTP version: expected 2, got {version}"));
    }

    let header_length = MINIMUM_HEADER_BYTES + usize::from(csrc_count) * 4;
    if data.len() < header_length {
        return Err(format!(
            "RTP data truncated: CC={csrc_count} requires {header_length} header bytes, but only {} bytes available",
            data.len()
        ));
    }
    if padding {
        return Err("RTP padding (P=1) is not supported by this parser".to_owned());
    }
    if extension {
        return Err("RTP header extension (X=1) is not supported by this parser".to_owned());
    }

    Ok(ParsedFields {
        version,
        padding,
        extension,
        csrc_count,
        marker,
        payload_type,
        sequence_number: u16::from_be_bytes([data[2], data[3]]),
        timestamp: u32::from_be_bytes([data[4], data[5], data[6], data[7]]),
        ssrc: u32::from_be_bytes([data[8], data[9], data[10], data[11]]),
        header_length,
        payload: &data[header_length..],
    })
}

/// Parsed RTP values without Pydantic conversion.
#[pyclass(frozen, module = "voip_rtp_native._native")]
struct ParsedRtp {
    #[pyo3(get)]
    version: u8,
    #[pyo3(get)]
    padding: bool,
    #[pyo3(get)]
    extension: bool,
    #[pyo3(get)]
    csrc_count: u8,
    #[pyo3(get)]
    marker: bool,
    #[pyo3(get)]
    payload_type: u8,
    #[pyo3(get)]
    sequence_number: u16,
    #[pyo3(get)]
    timestamp: u32,
    #[pyo3(get)]
    ssrc: u32,
    #[pyo3(get)]
    header_length: usize,
    payload: Vec<u8>,
}

#[pymethods]
impl ParsedRtp {
    #[getter]
    fn payload<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
        PyBytes::new(py, &self.payload)
    }

    fn __repr__(&self) -> String {
        format!(
            "ParsedRtp(payload_type={}, sequence_number={}, timestamp={}, ssrc={}, payload_bytes={})",
            self.payload_type,
            self.sequence_number,
            self.timestamp,
            self.ssrc,
            self.payload.len()
        )
    }
}

/// Parse one RTP packet using the same subset and errors as the Python parser.
#[pyfunction]
fn parse_rtp(data: &Bound<'_, PyBytes>) -> PyResult<ParsedRtp> {
    let fields = parse_fields(data.as_bytes()).map_err(PyValueError::new_err)?;
    Ok(ParsedRtp {
        version: fields.version,
        padding: fields.padding,
        extension: fields.extension,
        csrc_count: fields.csrc_count,
        marker: fields.marker,
        payload_type: fields.payload_type,
        sequence_number: fields.sequence_number,
        timestamp: fields.timestamp,
        ssrc: fields.ssrc,
        header_length: fields.header_length,
        payload: fields.payload.to_vec(),
    })
}

#[pymodule]
fn _native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_class::<ParsedRtp>()?;
    module.add_function(wrap_pyfunction!(parse_rtp, module)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixed_header(byte0: u8, byte1: u8, sequence: u16, timestamp: u32, ssrc: u32) -> Vec<u8> {
        let mut data = vec![byte0, byte1];
        data.extend_from_slice(&sequence.to_be_bytes());
        data.extend_from_slice(&timestamp.to_be_bytes());
        data.extend_from_slice(&ssrc.to_be_bytes());
        data
    }

    #[test]
    fn parses_minimum_packet_and_boundaries() {
        let data = fixed_header(0x80, 0xff, u16::MAX, u32::MAX, u32::MAX);
        let parsed = parse_fields(&data).unwrap();

        assert_eq!(parsed.version, 2);
        assert!(parsed.marker);
        assert_eq!(parsed.payload_type, 127);
        assert_eq!(parsed.sequence_number, u16::MAX);
        assert_eq!(parsed.timestamp, u32::MAX);
        assert_eq!(parsed.ssrc, u32::MAX);
        assert_eq!(parsed.header_length, 12);
        assert!(parsed.payload.is_empty());
    }

    #[test]
    fn csrc_list_moves_payload_offset() {
        let mut data = fixed_header(0x82, 0x00, 1, 160, 999);
        data.extend_from_slice(&100_u32.to_be_bytes());
        data.extend_from_slice(&200_u32.to_be_bytes());
        data.extend_from_slice(b"audio");

        let parsed = parse_fields(&data).unwrap();
        assert_eq!(parsed.csrc_count, 2);
        assert_eq!(parsed.header_length, 20);
        assert_eq!(parsed.payload, b"audio");
    }

    #[test]
    fn rejects_short_wrong_version_and_truncated_csrc() {
        assert!(parse_fields(&[0x80; 11]).unwrap_err().contains("too short"));
        assert!(
            parse_fields(&fixed_header(0x40, 0, 0, 0, 0))
                .unwrap_err()
                .contains("version")
        );

        let mut truncated = fixed_header(0x82, 0, 0, 0, 0);
        truncated.extend_from_slice(&[0, 0]);
        assert!(parse_fields(&truncated).unwrap_err().contains("truncated"));
    }

    #[test]
    fn rejects_padding_and_extension() {
        assert!(
            parse_fields(&fixed_header(0xa0, 0, 0, 0, 0))
                .unwrap_err()
                .contains("padding")
        );
        assert!(
            parse_fields(&fixed_header(0x90, 0, 0, 0, 0))
                .unwrap_err()
                .contains("extension")
        );
    }
}
