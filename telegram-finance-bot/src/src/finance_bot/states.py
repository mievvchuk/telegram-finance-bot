from aiogram.fsm.state import State, StatesGroup


class TransactionStates(StatesGroup):
    waiting_amount = State()


class CategoryStates(StatesGroup):
    waiting_new_name = State()
    waiting_rename = State()
