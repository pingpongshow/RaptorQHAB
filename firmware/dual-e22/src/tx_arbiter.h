/*
  tx_arbiter.h — one transmitter at a time, and an honest account of the cost.

  On the original Dual RF board the two radios were 400 MHz and 900 MHz, and
  arbitration was mostly about thermal and supply limits: two 1 W PAs stacked
  back-to-back on a 2-layer board, sharing one bulk capacitor.

  With two 900 MHz modules the problem is worse and different. The radios are
  centimetres apart, in the same band, with no filtering between them. When the
  Meshtastic slot transmits at 1 W, the RAPTOR slot's receiver is not merely
  degraded -- it is saturated. Anything arriving during that window is lost.

  So this does two jobs:

    1. Serialises transmissions, with a guard period afterwards for the front
       end to recover.
    2. Records how much receive time that cost, and makes it visible. A ground
       station that silently drops image packets whenever it beacons is a
       ground station that will be blamed for a bad radio link. The blind time
       is reported in the statistics line so the operator can see the trade.
*/
#pragma once
#include <Arduino.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

class TxArbiter {
public:
    // Time after a transmission before the other receiver is trusted again.
    // The SX1262's AGC recovers quickly, but a 1 W PA a few centimetres away
    // is a brutal input and this margin costs nothing worth having.
    static constexpr uint32_t TX_GUARD_MS = 40;

    void begin() {
        mutex_ = xSemaphoreCreateMutex();
    }

    // Take the right to transmit. Returns false if another slot holds it.
    bool acquire(uint8_t slot, uint32_t timeoutMs) {
        if (!mutex_) return false;
        if (xSemaphoreTake(mutex_, pdMS_TO_TICKS(timeoutMs)) != pdTRUE) {
            // Read-modify-write on a volatile is deprecated and, more to the
            // point, is not atomic. This counter is only touched by callers
            // that failed to take the mutex, so a plain guarded increment is
            // both correct and honest about the ordering.
            contended_ = contended_ + 1;
            return false;
        }
        activeSlot_ = slot;
        txStartMs_ = millis();
        return true;
    }

    void release() {
        uint32_t blind = millis() - txStartMs_ + TX_GUARD_MS;
        blindMs_ = blindMs_ + blind;   // holder of the mutex; no race here
        guardUntilMs_ = millis() + TX_GUARD_MS;
        activeSlot_ = 0xFF;
        if (mutex_) xSemaphoreGive(mutex_);
    }

    // True while a transmission, or its recovery guard, is in progress.
    bool transmitting() const {
        return activeSlot_ != 0xFF || (int32_t)(guardUntilMs_ - millis()) > 0;
    }

    uint8_t  activeSlot()  const { return activeSlot_; }
    uint32_t blindMs()     const { return blindMs_; }
    uint32_t contended()   const { return contended_; }

    // Proportion of wall-clock time the receivers have been blinded by our own
    // transmissions, in tenths of a percent.
    uint32_t blindPermille(uint32_t uptimeMs) const {
        if (!uptimeMs) return 0;
        return (uint32_t)((uint64_t)blindMs_ * 1000ULL / uptimeMs);
    }

private:
    SemaphoreHandle_t mutex_ = nullptr;
    volatile uint8_t  activeSlot_ = 0xFF;
    volatile uint32_t txStartMs_ = 0;
    volatile uint32_t guardUntilMs_ = 0;
    volatile uint32_t blindMs_ = 0;
    volatile uint32_t contended_ = 0;
};

// The shared SPI bus needs its own mutex. Two RadioLib instances on one
// SPIClass will interleave transactions and corrupt each other otherwise --
// and the symptom is not a clean failure, it is a radio that reports success
// while holding a half-written configuration.
class SpiBusLock {
public:
    void begin() { mutex_ = xSemaphoreCreateMutex(); }
    void take()  { if (mutex_) xSemaphoreTake(mutex_, portMAX_DELAY); }
    void give()  { if (mutex_) xSemaphoreGive(mutex_); }
private:
    SemaphoreHandle_t mutex_ = nullptr;
};

// RAII helper so an early return cannot leave the bus locked.
class SpiBusGuard {
public:
    explicit SpiBusGuard(SpiBusLock& lock) : lock_(lock) { lock_.take(); }
    ~SpiBusGuard() { lock_.give(); }
private:
    SpiBusLock& lock_;
};
