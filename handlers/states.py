from aiogram.fsm.state import State, StatesGroup


class SetAmount(StatesGroup):
    waiting_for_amount = State()


class SetMinProfit(StatesGroup):
    waiting_for_min_profit = State()


class SetRisk(StatesGroup):
    waiting_for_risk = State()


class SetFilters(StatesGroup):
    waiting_for_banks = State()
    waiting_for_exchanges = State()
    waiting_for_network = State()
    waiting_for_interval = State()


class SetBankFee(StatesGroup):
    waiting_for_bank_fee = State()


class AddParticipant(StatesGroup):
    waiting_for_user_id = State()
