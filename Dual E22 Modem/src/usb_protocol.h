/*
  usb_protocol.h — the wire format to the ground station.

  RAPTOR frames are byte-for-byte identical to the Heltec T190's output, so the
  existing macOS app and Python ground station decode them without a change.
  That compatibility is deliberate: swapping the modem should not require
  swapping the software that talks to it.

    0x7E [LEN_HI][LEN_LO][RSSI_I][RSSI_F][SNR_I][SNR_F][DATA...][CKSUM] 0x7E

  Meshtastic frames carry a different delimiter, 0x7B. An older ground station
  splits its input on 0x7E and simply never sees them; a newer one parses both.
  Adding a type byte inside the existing frame would have been tidier and would
  have broken every existing parser.

  Both frame types use the same byte stuffing: 0x7E -> 0x7D 0x5E and
  0x7D -> 0x7D 0x5D, applied to every byte including the header.
*/
#pragma once
#include <Arduino.h>

#define FRAME_RAPTOR      0x7E
#define FRAME_MESHTASTIC  0x7B
#define FRAME_ESCAPE      0x7D

inline void writeStuffed(Stream& out, uint8_t b) {
    if (b == FRAME_RAPTOR)      { out.write((uint8_t)FRAME_ESCAPE); out.write((uint8_t)0x5E); }
    else if (b == FRAME_MESHTASTIC) { out.write((uint8_t)FRAME_ESCAPE); out.write((uint8_t)0x5B); }
    else if (b == FRAME_ESCAPE) { out.write((uint8_t)FRAME_ESCAPE); out.write((uint8_t)0x5D); }
    else                        { out.write(b); }
}

// Emit one framed packet. `delimiter` selects which stream it belongs to.
inline void sendFrame(Stream& out, uint8_t delimiter,
                      const uint8_t* data, size_t len,
                      float rssi, float snr) {
    uint8_t lenHi   = (len >> 8) & 0xFF;
    uint8_t lenLo   = len & 0xFF;
    int8_t  rssiInt = (int8_t)rssi;
    uint8_t rssiFrac = (uint8_t)(fabsf(rssi - rssiInt) * 100.0f);
    int8_t  snrInt  = (int8_t)snr;
    uint8_t snrFrac = (uint8_t)(fabsf(snr - snrInt) * 100.0f);

    uint8_t checksum = lenHi ^ lenLo ^ (uint8_t)rssiInt ^ rssiFrac
                     ^ (uint8_t)snrInt ^ snrFrac;
    for (size_t i = 0; i < len; i++) checksum ^= data[i];

    out.write(delimiter);
    writeStuffed(out, lenHi);
    writeStuffed(out, lenLo);
    writeStuffed(out, (uint8_t)rssiInt);
    writeStuffed(out, rssiFrac);
    writeStuffed(out, (uint8_t)snrInt);
    writeStuffed(out, snrFrac);
    for (size_t i = 0; i < len; i++) writeStuffed(out, data[i]);
    writeStuffed(out, checksum);
    out.write(delimiter);
    out.flush();
}
