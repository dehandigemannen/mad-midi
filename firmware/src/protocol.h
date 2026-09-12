#pragma once
#include <Arduino.h>

void protocolInit();
void protocolUpdate(); // call every loop(): reads available Serial bytes, dispatches per current mode
