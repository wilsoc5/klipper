#ifndef __SAMD21_INTERNAL_H
#define __SAMD21_INTERNAL_H
// Local definitions for samd21 code

#include <stdint.h> // uint32_t

void enable_pclock(uint32_t clock_id, uint32_t pmask);
void gpio_peripheral(char bank, uint32_t bit, char ptype, uint32_t pull_up);

#endif // internal.h
