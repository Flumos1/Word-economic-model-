"""Executable tests for scoped four-sector SFC core."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import unittest
from amfe.accounting_core import Accounts,AccountingError,D,Transaction,SECTORS,cash_transfer,credit,government_bond,produce,purchase,interest,invest,depreciate

class TestSFC(unittest.TestCase):
    def basic(self):
        a=Accounts()
        for t in (credit(100),produce(100),cash_transfer("wages","firm","household",100),purchase("household",70),cash_transfer("levy","firm","government",30),cash_transfer("transfer","government","household",10),purchase("government",15),interest(5),credit(50,True)):a.apply(t)
        return a
    def test_baseline_matrix(self):
        a=self.basic();p=a.close()
        self.assertEqual(p["closing"],{"household":D(40),"firm":D(-35),"bank":D(5),"government":D(5)})
        self.assertEqual(sum(p["capital"].values()),D(15))
        self.assertEqual(sum(p["financial"].values()),D(0))
        self.assertEqual(len(p["matrix"]),9)
        for row in p["matrix"]:
            self.assertEqual(sum(row["financial"].values()),0)
            for s in SECTORS:self.assertEqual(row["current"][s],row["capital"][s]+row["financial"][s])
    def test_two_periods(self):
        a=Accounts()
        for t in (credit(100),produce(100),cash_transfer("wages","firm","household",100),purchase("household",80)):a.apply(t)
        p1=a.close();a.begin("P2");a.apply(purchase("household",10));p2=a.close()
        self.assertEqual(p1["closing"],p2["opening"])
        self.assertEqual(p2["capital"]["firm"],-10)
        self.assertEqual(a.real[("firm","inventory")],10)
    def test_bond(self):
        a=Accounts()
        for t in (credit(100),government_bond(20),produce(100),cash_transfer("wages","firm","household",100),purchase("household",70),cash_transfer("levy","firm","government",5),cash_transfer("transfer","government","household",10),purchase("government",15),credit(80,True)):a.apply(t)
        self.assertEqual(a.financial[("government","bond")],-20)
        self.assertEqual(a.financial[("government","deposit")],0)
        a.close()
    def test_investment_depreciation(self):
        a=Accounts()
        for t in (produce(20),invest(12),depreciate(2)):a.apply(t)
        p=a.close()
        self.assertEqual(a.real[("firm","inventory")],8)
        self.assertEqual(a.real[("firm","fixed_capital")],10)
        self.assertEqual(p["current"]["firm"],18)
        self.assertEqual(p["capital"]["firm"],18)
    def test_atomicity(self):
        a=self.basic();a.close();a.begin("P2");old=(a.financial.copy(),a.real.copy(),len(a.events))
        bad=(purchase("household",1000),credit(1000,True),invest(1000),
             Transaction("wages",{"firm":1},{},{("firm","deposit"):1}),
             Transaction("wages",{},{},{("firm","deposit"):-1,("government","deposit"):1}),
             Transaction("wages",{},{},{("bank","loan"):1,("firm","loan"):-1}))
        for t in bad:
            with self.assertRaises(AccountingError):a.apply(t)
            self.assertEqual((a.financial,a.real,len(a.events)),old)
    def test_no_post_after_close(self):
        a=self.basic();a.close();old=(a.financial.copy(),a.real.copy(),len(a.events))
        with self.assertRaises(AccountingError):a.apply(produce(1))
        self.assertEqual((a.financial,a.real,len(a.events)),old)
    def test_decimal_nonfinite(self):
        a=Accounts()
        for t in (credit("10.25"),produce("10.25"),cash_transfer("wages","firm","household","10.25"),purchase("household","4.10")):a.apply(t)
        self.assertEqual(a.real[("firm","inventory")],D("6.15"))
        for x in ("NaN","Infinity",True):
            with self.assertRaises(AccountingError):produce(x)
    def test_parameter_grid_432(self):
        count=0
        for sales in range(50,90,5):
            for levy in range(0,25,3):
                for public in range(0,11,2):
                    a=Accounts()
                    for t in (credit(100),produce(100),cash_transfer("wages","firm","household",100),purchase("household",sales),cash_transfer("levy","firm","government",levy)):a.apply(t)
                    tr=levy//2
                    if tr:a.apply(cash_transfer("transfer","government","household",tr))
                    borrowing=max(0,public+tr-levy)
                    if borrowing:a.apply(government_bond(borrowing))
                    a.apply(purchase("government",public))
                    a.apply(credit(min(D(100),a.financial.get(("firm","deposit"),D(0))),True))
                    a.close();count+=1
        self.assertEqual(count,432)

if __name__=="__main__":unittest.main(verbosity=2)
